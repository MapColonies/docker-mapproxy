function jwt(data) {
    var parts = data.split('.').slice(0,2)
        .map(v=>Buffer.from(v, 'base64url').toString())
        .map(JSON.parse);
    return { headers:parts[0], payload: parts[1] };
}

function jwt_payload_sub(r) {
    console.log('jwt_payload', r.headersIn)
    return jwt(r);
}


export default {jwt_payload_sub}
