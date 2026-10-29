// Decrypts challenge subdomain labels issued by the Docker CTFd plugin
// (encode_route_label in __init__.py) and returns the instance's host port,
// or "" when the label is invalid or expired. Keep in sync with docker_route.lua.
//
// nginx with njs:
//   js_import docker_route from /etc/nginx/njs/docker_route.js;
//   js_set $chal_port docker_route.port;
//   js_var $docker_route_key "<DOCKER_CHALLENGE_ROUTE_KEY>";
import crypto from 'crypto';

const ALPHABET = 'abcdefghijklmnopqrstuvwxyz234567';
const ROUTE_EPOCH = 1735689600; // 2025-01-01 UTC; expiry is stored as minutes since then

function hmac(key, data) {
    return crypto.createHmac('sha256', key).update(data).digest();
}

function base32(label) {
    const out = [];
    let value = 0;
    let bits = 0;
    // Index loop: njs's default engine has no for...of.
    for (let i = 0; i < label.length; i++) {
        const index = ALPHABET.indexOf(label[i]);
        if (index < 0) {
            return null;
        }
        value = (value << 5) | index;
        bits += 5;
        if (bits >= 8) {
            bits -= 8;
            out.push((value >> bits) & 0xff);
            value &= (1 << bits) - 1;
        }
    }
    return Buffer.from(out);
}

function port(r) {
    // nginx has already lowercased $host.
    const raw = base32(r.variables.host.split('.')[0]);
    if (!raw || raw.length !== 10) {
        return '';
    }

    const key = r.variables.docker_route_key;
    const tag = raw.slice(0, 5);
    const stream = hmac(hmac(key, 'docker-route-enc'), tag);
    const plain = Buffer.alloc(5);
    for (let i = 0; i < 5; i++) {
        plain[i] = raw[5 + i] ^ stream[i];
    }

    const expected = hmac(hmac(key, 'docker-route-mac'), plain);
    let diff = 0;
    for (let i = 0; i < 5; i++) {
        diff |= expected[i] ^ tag[i];
    }
    const expiresAt = ROUTE_EPOCH + ((plain[2] << 16) | (plain[3] << 8) | plain[4]) * 60;
    if (diff || expiresAt < Math.floor(Date.now() / 1000)) {
        return '';
    }
    return String(plain.readUInt16BE(0));
}

export default { port };
