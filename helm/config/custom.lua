-------------------------------------------------------------------------------
-- Helper Functions
-------------------------------------------------------------------------------

local function url_decode(str)
    if not str then return "" end
    local decoded = str:gsub("+", " "):gsub("%%(%x%x)", function(h)
        return string.char(tonumber(h, 16))
    end)
    return decoded
end

local function parse_query_string(query)
    local params = {}
    if not query or query == "" then return params end
    for k, v in query:gmatch("([^&]+)=([^&]+)") do
        params[string.lower(k)] = url_decode(v)
    end
    return params
end

local function normalize_format(format_str)
    if not format_str then return nil end
    -- "image/png; mode=8bit" -> "png". Parenthesized so gsub's match count isn't returned too.
    return (string.lower(format_str):gsub("^image/", ""):gsub("%s*;.*$", ""))
end

-- Operation and format come straight from the client, so anything outside these sets is
-- collapsed to "other" - otherwise junk input (?REQUEST=<random>) mints a new series per request.
local KNOWN_OPERATIONS = {
    GETMAP = true, GETFEATUREINFO = true, GETLEGENDGRAPHIC = true,
    GETCAPABILITIES = true, GETTILE = true,
}

local KNOWN_FORMATS = {
    png = true, png8 = true, jpeg = true, jpg = true, gif = true,
    webp = true, tiff = true, xml = true, json = true,
}

local function bounded(value, allowed)
    if value == nil then return "none" end
    if allowed[value] then return value end
    return "other"
end

-------------------------------------------------------------------------------
-- Parsers (KVP and RESTful)
-------------------------------------------------------------------------------

local function parse_kvp(params)
    local service   = params["service"] and string.upper(params["service"])
    local operation = params["request"] and string.upper(params["request"])
    local layer     = params["layers"] or params["layer"]
    local format    = normalize_format(params["format"])
    local zoom      = nil

    if params["tilematrix"] then
        zoom = params["tilematrix"]:match(":(%d+)$") or
              params["tilematrix"]:match("^(%d+)$") or
              params["tilematrix"]
    end

    if not service and operation then
        if operation == "GETMAP" or operation == "GETFEATUREINFO" then
            service = "WMS"
        elseif operation == "GETTILE" then
            service = "WMTS"
        end
    end

    return service, operation, layer, zoom, format
end

local function parse_restful(path)
    local lower_path = string.lower(path)

    if lower_path:find("/wmts/") then
        local pattern = "/wmts/([^/]+)/([^/]+)/(%d+)/(%d+)/(%d+)%.(%w+)"
        local layer, grid, zoom, x, y, format = path:match(pattern)

        if layer then
            return "WMTS", "GETTILE", layer, zoom, normalize_format(format)
        elseif lower_path:find("wmtscapabilities%.xml$") then
            return "WMTS", "GETCAPABILITIES", nil, nil, "xml"
        end
    end

    return nil, nil, nil, nil, nil
end

-- Bucket a raw zoom level so the WMTS counter's cardinality doesn't scale with
-- every individual zoom on top of layer count: 0-14 (overview/regional),
-- 15-18 (street level), 19+ (max detail).
local function zoom_group(zoom)
    local z = tonumber(zoom)
    if z == nil then return "none" end
    if z <= 14 then return "0-14" end
    if z <= 18 then return "15-18" end
    return "19+"
end

-------------------------------------------------------------------------------
-- Main Fluent Bit Entrypoint
-------------------------------------------------------------------------------

function parse_mapproxy_request(tag, timestamp, record)
    -- The URL lives at Attributes["url.path"] / Attributes["url.query"] -
    -- Fluent Bit's json parser never produces a top-level "request_uri".
    local attrs = record["Attributes"] or {}
    local path         = attrs["url.path"] or ""
    local query_string = attrs["url.query"] or ""

    local params = parse_query_string(query_string)

    local service, operation, layer, zoom, format

    if query_string ~= "" then
        service, operation, layer, zoom, format = parse_kvp(params)
    end

    if not service then
        service, operation, layer, zoom, format = parse_restful(path)
    end

    if not service or not operation then
        return 0, 0, 0
    end

    -- Both come off nginx as strings ("200", "0.043").
    local status = tonumber(attrs["http.response.status_code"])

    -- client_name is only logged when nginx.authorization is enabled. On 401/403 the token
    -- was rejected, so neither its subject nor the requested layer can be trusted as a label;
    -- any other status means auth_request passed, so both are kept (incl. 400/404).
    local client = attrs["mapcolonies.http.auth.token.client_name"]
    if client == nil or client == "" then client = "anonymous" end
    if status == 401 or status == 403 then
        client = "unauthorized"
        layer  = "unauthorized"
    end

    record["ogc_service"]    = service
    record["ogc_operation"]  = bounded(operation, KNOWN_OPERATIONS)
    record["ogc_layer"]      = layer  or "none"
    record["ogc_zoom"]       = zoom   or "none"
    record["ogc_format"]     = bounded(format, KNOWN_FORMATS)
    record["ogc_zoom_group"] = zoom_group(zoom)
    record["client_name"]    = client

    -- result (ok/error) + a numeric request_time, for the latency histograms below.
    record["result"] = (status ~= nil and status >= 400) and "error" or "ok"

    local request_time = tonumber(attrs["mapcolonies.request_time"])
    if request_time ~= nil then
        record["request_time_seconds"] = request_time
    end

    return 1, timestamp, record
end
