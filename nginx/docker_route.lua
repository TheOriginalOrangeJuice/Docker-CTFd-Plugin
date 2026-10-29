-- Decrypts challenge subdomain labels issued by the Docker CTFd plugin
-- (encode_route_label in __init__.py) and returns the instance's host port,
-- or "" when the label is invalid or expired. Keep in sync with docker_route.js.
--
-- Nginx Proxy Manager / OpenResty, in the proxy host's Advanced tab:
--   set $docker_route_key "<DOCKER_CHALLENGE_ROUTE_KEY>";
--   set_by_lua_file $port /data/nginx/custom/docker_route.lua;
--   if ($port = "") { return 404; }

local bit = require "bit"
local sha256 = require "resty.sha256"

local ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
local ROUTE_EPOCH = 1735689600 -- 2025-01-01 UTC; expiry is stored as minutes since then

local function hash(data)
    local h = sha256:new()
    h:update(data)
    return h:final()
end

local function hmac(key, data)
    if #key > 64 then
        key = hash(key)
    end
    local inner, outer = {}, {}
    for i = 1, 64 do
        local byte = key:byte(i) or 0
        inner[i] = string.char(bit.bxor(byte, 0x36))
        outer[i] = string.char(bit.bxor(byte, 0x5c))
    end
    return hash(table.concat(outer) .. hash(table.concat(inner) .. data))
end

local function base32(label)
    local out, value, bits = {}, 0, 0
    for i = 1, #label do
        local index = ALPHABET:find(label:sub(i, i), 1, true)
        if not index then
            return nil
        end
        value = bit.bor(bit.lshift(value, 5), index - 1)
        bits = bits + 5
        if bits >= 8 then
            bits = bits - 8
            out[#out + 1] = string.char(bit.band(bit.rshift(value, bits), 0xff))
            value = bit.band(value, bit.lshift(1, bits) - 1)
        end
    end
    return table.concat(out)
end

-- nginx has already lowercased $host.
local raw = base32(ngx.var.host:match("^[^.]+"))
if not raw or #raw ~= 10 then
    return ""
end

local key = ngx.var.docker_route_key
local tag, cipher = raw:sub(1, 5), raw:sub(6, 10)
local stream = hmac(hmac(key, "docker-route-enc"), tag)
local plain = {}
for i = 1, 5 do
    plain[i] = string.char(bit.bxor(cipher:byte(i), stream:byte(i)))
end
plain = table.concat(plain)

local expected = hmac(hmac(key, "docker-route-mac"), plain)
local diff = 0
for i = 1, 5 do
    diff = bit.bor(diff, bit.bxor(expected:byte(i), tag:byte(i)))
end
if diff ~= 0 then
    return ""
end

local b1, b2, b3, b4, b5 = plain:byte(1, 5)
if ROUTE_EPOCH + ((b3 * 256 + b4) * 256 + b5) * 60 < ngx.time() then
    return ""
end
return tostring(b1 * 256 + b2)
