function jwt(data) {
    r.return(200, data, "Hello world!\n");
    var parts = data.split('.').slice(0,2)
        .map(v=>Buffer.from(v, 'base64url').toString())
        .map(JSON.parse);
    return { headers:parts[0], payload: parts[1] };
}

function jwt_payload_sub(r) {
    return jwt(r);
}


export default {jwt_payload_sub}
