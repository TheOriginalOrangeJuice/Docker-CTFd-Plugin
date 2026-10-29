# Docker Challenge Plugin for CTFd

Adds a Docker-backed challenge type to CTFd. Players can start an isolated instance of a challenge container or compose stack, receive a dedicated connection port, and stop or revert their own environment without affecting other competitors.

The plugin publishes challenge ports from that same host. By default, players connect through an encrypted per-instance subdomain such as `k7m2q5x3t9wz4bq2.ctf.example.com`. Your reverse proxy decrypts the subdomain and routes the request to the right port. The original `HOST:PORT` style is still available as a toggle (see [Player Access Mode](#player-access-mode)).

## Screenshots

### Admin Docker Configuration

![Admin Docker configuration](docs/screenshots/admin-docker-config.png)

### Admin Challenge Creation

![Admin create Docker challenge](docs/screenshots/admin-create-docker-challenge.png)

### Player Challenge View

![Player Docker challenge modal](docs/screenshots/player-docker-challenge.png)

### Admin Instance Status

![Admin Docker status](docs/screenshots/admin-docker-status.png)


## Current Setup does the following:

- CTFd talks to Docker through the internal proxy
- Players connect to the same public hostname they already use for CTFd
- Challenge containers still run on the same machine as CTFd

## Installation

1. Copy the plugin folder into your CTFd plugins directory:

```text
CTFd/plugins/docker_challenges
```

2. If you build CTFd as a Docker image, rebuild the image so the plugin requirements are installed.

3. Start or restart CTFd.

4. Open the plugin configuration page:

```text
/admin/docker_config
```

5. Open the runtime status page when needed:

```text
/admin/docker_status
```

## Recommended Docker Proxy Setup

For a single-host deployment, use a local `docker-socket-proxy` service and keep it on an internal Docker network with CTFd.

Example `docker-compose.yml` excerpt:

```yaml
services:
  ctfd:
    image: your-ctfd-image
    depends_on:
      - dockerproxy
    environment:
      DOCKER_CHALLENGE_PORT_MIN: 30000
      DOCKER_CHALLENGE_PORT_MAX: 34000
      DOCKER_CHALLENGE_PUBLIC_HOST: ctf.example.com
      DOCKER_CHALLENGE_HMAC_SECRET: replace-this-secret
    networks:
      - default
      - docker_internal

  dockerproxy:
    image: ghcr.io/tecnativa/docker-socket-proxy:latest
    restart: unless-stopped
    environment:
      CONTAINERS: 1
      IMAGES: 1
      NETWORKS: 1
      POST: 1
      ALLOW_START: 1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docker_internal

networks:
  docker_internal:
    internal: true
```

After the stack is up, configure the plugin with:

- **Docker Host**: `dockerproxy:2375`
- **TLS Enabled**: `No`


## Plugin Configuration in CTFd

Open `/admin/docker_config` and configure:

- **Docker Host**
  Use the proxy hostname and port, for example `dockerproxy:2375`.
- **Subdomain Mode**
  `Yes` (default) shows players an encrypted subdomain such as `k7m2q5x3t9wz4bq2.ctf.example.com`; `No` shows `ctf.example.com:36753`. See [Player Access Mode](#player-access-mode).
- **Revert Cooldown**
  Minimum wait before the same player or team can reset the same challenge instance.
- **Instance TTL**
  Maximum lifetime for an active instance before automatic cleanup.
- **Max Active Challenges**
  Maximum number of active Docker challenge instances per player or team.
- **Allowed Repositories**
  Optional image allow-list shown to challenge authors.

## Host and Server Configuration

### Reserve a Dedicated Port Range

Pick a host port range only for Docker challenges.

Example:

```text
30000-34000
```

Set the same range for the plugin with:

```bash
DOCKER_CHALLENGE_PORT_MIN=30000
DOCKER_CHALLENGE_PORT_MAX=34000
```

Allow that range through the host firewall or cloud firewall rules.

### Set the Public Hostname

By default, the plugin uses the current request hostname for player connection details.

If CTFd is behind a reverse proxy or the public hostname should be fixed explicitly, set:

```bash
DOCKER_CHALLENGE_PUBLIC_HOST=ctf.example.com
```

### Player Access Mode

The **Subdomain Mode** toggle on `/admin/docker_config` controls what players see for each published port:

| Subdomain Mode | Player sees |
| --- | --- |
| Yes (default) | `k7m2q5x3t9wz4bq2.ctf.example.com` |
| No | `ctf.example.com:36753` |

In subdomain mode, the 16-character label is the instance's host port and expiry time, encrypted and authenticated with a key that CTFd shares with your reverse proxy. The proxy decrypts the label and forwards the request to that port on the challenge host. Labels that fail to decrypt, are tampered with, or have expired get a 404. Players can't work out another team's label from their own, so they can't reach each other's instances by guessing URLs.

#### How it behaves

- **The key.** Generate it once with `openssl rand -base64 32`. Set it as `DOCKER_CHALLENGE_ROUTE_KEY` on CTFd and give the same value to the proxy. If the key isn't set, new instances use `host:port`. Changing the key breaks the URLs of instances that are already running.
- **Expiry.** A label is valid until its instance's TTL runs out, counted from when the instance started.
- **Port holds.** Labels can't be revoked early. To stop an old label from reaching a different team's instance later, a stopped instance's ports aren't handed out again until its labels expire. Each start therefore reserves its ports for up to one TTL. Make the port range large enough for the number of starts you expect in one TTL window.
- **New instances only.** The toggle applies to newly started instances. Running instances keep the address they started with, and Compose environment values are filled in at start.
- **HTTP only.** Only HTTP(S) and WebSocket traffic can go through the proxy. Raw TCP (`nc`) and UDP challenges need host:port mode.
- **Close the direct path.** Players must not be able to reach the challenge port range directly. If they could, they could scan the ports and skip the labels entirely. Either firewall the range so that only the proxy can reach it (setups A and B), or publish the ports on loopback (setup C).
- **DNS and TLS.** Point a wildcard DNS record such as `*.ctf.example.com` at the proxy, and serve a wildcard certificate. Let's Encrypt only issues wildcard certificates through the DNS-01 challenge; a CDN in front of the proxy can also terminate TLS.

The proxy-side decoders live in this plugin's `nginx/` folder:
- `docker_route.lua` for OpenResty and Nginx Proxy Manager
- `docker_route.js` for stock nginx with the njs module

#### Setup A: Nginx Proxy Manager on a separate host

1. Copy `nginx/docker_route.lua` into Nginx Proxy Manager's data volume as `/data/nginx/custom/docker_route.lua`.
2. Add a **Proxy Host**:
   - **Domain Names:** `*.ctf.example.com`
   - **Scheme:** `http`
   - **Forward Hostname / IP:** the CTFd host, for example `10.0.0.5`
   - **Forward Port:** any value, since the snippet below replaces it for each request
   - Turn on **Websockets Support**, and choose your wildcard certificate on the **SSL** tab.
3. On the **Advanced** tab, add:

   ```nginx
   set $docker_route_key "paste the DOCKER_CHALLENGE_ROUTE_KEY value here";
   set_by_lua_file $port /data/nginx/custom/docker_route.lua;
   if ($port = "") { return 404; }
   ```

   Nginx Proxy Manager forwards to the configured host and `$port`. The snippet replaces `$port` with the port decrypted from the label.

#### Setup B: plain nginx on a separate host

nginx's official packages and Docker images include the njs module (`ngx_http_js_module`). Copy `nginx/docker_route.js` to `/etc/nginx/njs/docker_route.js`, then:

```nginx
# Top of nginx.conf, outside http {}
load_module modules/ngx_http_js_module.so;

http {
    js_import docker_route from /etc/nginx/njs/docker_route.js;
    js_set $chal_port docker_route.port;
    # Keep this line in an include file that only root can read.
    js_var $docker_route_key "paste the DOCKER_CHALLENGE_ROUTE_KEY value here";

    server {
        listen 443 ssl;
        # The quotes are required because of the {16}.
        server_name "~^[a-z2-7]{16}\.ctf\.example\.com$";

        ssl_certificate     /etc/ssl/wildcard.ctf.example.com/fullchain.pem;
        ssl_certificate_key /etc/ssl/wildcard.ctf.example.com/privkey.pem;

        if ($chal_port = "") { return 404; }

        location / {
            proxy_pass http://10.0.0.5:$chal_port;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
}
```

#### Setup C: everything on one host

Use setup B with three changes:

- Set `DOCKER_CHALLENGE_BIND_IP=127.0.0.1` on CTFd. Challenge ports are then published on loopback only, so nothing on the network can reach them.
- Proxy to `http://127.0.0.1:$chal_port`.
- Run nginx on the host itself, or in a container with `network_mode: host`. Inside a bridged container, `127.0.0.1` is the container, not the host. If you switch CTFd's bundled nginx service to host networking, point its CTFd upstream at `127.0.0.1:8000`.

### Size Docker Networks for Compose Challenges

Each running Compose stack uses one bridge network. For **X** concurrent teams, choose a nonoverlapping pool with more than X subnets to leave room for other Docker networks. A base prefix `/B` split with `size: 24` provides up to `2^(24-B)` networks; the example below provides up to 4,096 `/24` networks and supported our 70-team test.

```json
{
  "default-address-pools": [
    { "base": "10.80.0.0/12", "size": 24 }
  ]
}
```

Add this to `/etc/docker/daemon.json` and restart Docker during maintenance.

### Pre-Pull Challenge Images

Pull challenge images onto the host before the event so launches are fast and predictable.

### Optional Global Resource Limits

You can apply default limits to all launched services:

```bash
DOCKER_CHALLENGE_MEMORY_LIMIT=512m
DOCKER_CHALLENGE_CPU_LIMIT=1.0
```

These act as defaults. Compose services can also set `mem_limit` and `cpus` individually.

## Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DOCKER_CHALLENGE_PORT_MIN` | First host port available for challenge instances | `30000` |
| `DOCKER_CHALLENGE_PORT_MAX` | Last host port available for challenge instances | `60000` |
| `DOCKER_CHALLENGE_PUBLIC_HOST` | Player-facing hostname for connection details | request host |
| `DOCKER_CHALLENGE_ROUTE_KEY` | Shared key for encrypted subdomain labels; give the reverse proxy the same value | unset (host:port) |
| `DOCKER_CHALLENGE_BIND_IP` | Host IP that challenge ports are published on, for example `127.0.0.1` when the proxy runs on the same host | all interfaces |
| `DOCKER_CHALLENGE_HMAC_SECRET` | Secret used for per-participant HMAC flag generation | CTFd `SECRET_KEY` |
| `DOCKER_CHALLENGE_REQUEST_TIMEOUT` | Docker API timeout in seconds | `10` |
| `DOCKER_CHALLENGE_REAPER_INTERVAL` | Background cleanup interval in seconds | `60` |
| `DOCKER_CHALLENGE_REVERT_COOLDOWN` | Default revert cooldown if not set in the admin UI | `300` |
| `DOCKER_CHALLENGE_CONTAINER_TTL` | Default instance TTL if not set in the admin UI | `7200` |
| `DOCKER_CHALLENGE_MAX_ACTIVE` | Default max active instances if not set in the admin UI | `0` |
| `DOCKER_CHALLENGE_MEMORY_LIMIT` | Default memory limit for launched services | unset |
| `DOCKER_CHALLENGE_CPU_LIMIT` | Default CPU limit for launched services | unset |
| `DOCKER_CHALLENGE_MAX_SERVICES` | Max services allowed in one compose challenge | `8` |
| `DOCKER_CHALLENGE_MAX_PUBLISHED_PORTS` | Max published ports allowed for one challenge instance | `16` |
| `DOCKER_CHALLENGE_PORT_RETRIES` | Port allocation retry attempts when Docker reports collisions | `5` |

`0` for `DOCKER_CHALLENGE_MAX_ACTIVE` means unlimited active Docker challenges per player or team.

## Creating Challenges

Go to **Admin Panel -> Challenges -> New Challenge** and select the `docker` type.

### Single Image Mode

Use this mode when one image is enough.

Requirements:

- The image must already exist on the host
- The image must expose at least one port in its Docker metadata
- Select which exposed container ports should be published to players. The plugin assigns the host-side ports automatically for each instance.

### Compose Stack Mode

Use this mode when the challenge needs multiple services.

Each launched compose stack gets:

- its own isolated Docker bridge network
- its own copies of every service in the stack
- random published host ports only for services that define `ports`

Services in the same stack can reach each other by service name, such as `http://api:5000`.

For ports, the important rule is:

- Put the container port your service listens on in `ports`
- The plugin chooses the host port automatically when the player launches the challenge

That means these two examples behave the same in this plugin:

```yaml
ports:
  - "1339"
```

```yaml
ports:
  - "1339:1339"
```

In both cases, the service listens on port `1339` inside the container, and the plugin replaces the host-side port with a random available port from the configured challenge range.

Example stack:

```yaml
services:
  portal:
    image: acme-ticketing-portal:latest
    depends_on:
      - api
    ports:
      - "8080"
    environment:
      PORT: "8080"
      PUBLIC_BASE_URL: "http://{{ENDPOINT}}"
      API_URL: "http://api:5000"
    entrypoint: /app/start-portal.sh

  api:
    image: acme-ticketing-api:latest
    depends_on:
      - review
    environment:
      PORT: "5000"
      REVIEW_URL: "http://review:7000"
    entrypoint: /app/start-api.sh

  review:
    image: acme-ticketing-review:latest
    environment:
      PORT: "7000"
      # Standard flag mode only: keep the shared flag in the variable your challenge already expects
      FLAG: "RCC{example_shared_flag}"
      # HMAC flag mode: remove the FLAG line above and make the service read CTFD_FLAG directly
    entrypoint: /app/start-review.sh
```

When a player launches this stack:

- The plugin keeps `portal` listening on container port `8080`.
- The plugin chooses a random host port for that service. If the chosen host port is `12345`, the result behaves like `12345:8080`.
- `{{PORT}}` becomes that published host port, such as `12345`.
- `{{ENDPOINT}}` becomes the address players use for that port: an encrypted subdomain such as `k7m2q5x3t9wz4bq2.ctf.example.com` in subdomain mode, or `ctf.example.com:12345` otherwise. That makes `PUBLIC_BASE_URL` correct in both modes.
- The base hostname is `DOCKER_CHALLENGE_PUBLIC_HOST` if you set it, otherwise the current CTFd hostname from the request.
- The player sees the same address in the challenge view.

Supported service fields are listed below. Other service fields are rejected so an unsupported Compose option cannot be silently ignored.

- `image`
- `ports`
- `environment`
- `depends_on`
- `command`
- `entrypoint`
- `cpus`
- `mem_limit`
- `cap_drop`
- `read_only`
- `pids_limit`

Common unsupported host-level options are rejected, including:

- `build`
- `privileged`
- `network_mode`
- `volumes`
- `devices`

Compose files are validated when the challenge is saved. Service names, ports, protocols, dependencies, environment entries, and resource values must be valid; dependency cycles and Compose files larger than 128 KB are rejected.

### Compose Placeholders

Compose environment values, command strings, and entrypoint strings can use:

- `{{HOST}}`
- `{{PORT}}`
- `{{ENDPOINT}}`
- `{{PORT_service}}`
- `{{PORT_service_80}}`
- `{{ENDPOINT_service}}`
- `{{ENDPOINT_service_80}}`

Examples, for a stack where service `web` publishes target port `80` on host port `36753`, and `k7m2…` stands for that port's encrypted label:

| Placeholder | Subdomain mode | Host:Port mode |
| --- | --- | --- |
| `{{HOST}}` (in `web`) | `k7m2….ctf.example.com` | `ctf.example.com` |
| `{{PORT}}` (in `web`) | `36753` | `36753` |
| `{{ENDPOINT}}` (in `web`) | `k7m2….ctf.example.com` | `ctf.example.com:36753` |
| `{{PORT_web}}`, `{{PORT_web_80}}` | `36753` | `36753` |
| `{{ENDPOINT_web}}`, `{{ENDPOINT_web_80}}` | `k7m2….ctf.example.com` | `ctf.example.com:36753` |

`{{HOST}}`, `{{PORT}}` and `{{ENDPOINT}}` refer to the current service's first published port. In a service with no published ports, `{{HOST}}` and `{{ENDPOINT}}` become the base hostname and `{{PORT}}` is empty. To reach another service's public address, use `{{ENDPOINT_service}}`.

The challenge's **Connection Info** field supports the same `{{HOST}}`, `{{PORT}}` and `{{ENDPOINT}}` placeholders, plus `{{PORT_80}}` and `{{ENDPOINT_80}}` by target port for Compose stacks. For example, `https://{{ENDPOINT}}` becomes a clickable `https://k7m2q5x3t9wz4bq2.ctf.example.com` in subdomain mode, and `https://ctf.example.com:36753` in host:port mode. `nc {{HOST}} {{PORT}}` is only useful in host:port mode, because the proxy carries HTTP only.

### Flag Modes

You can choose either:

- **Standard CTFd Flags**
- **Per-Participant HMAC Flag**

Use standard flags when the challenge should behave like a normal CTFd challenge and every participant submits the same flag value.

Use HMAC mode when each player or team should receive a different flag value for the same challenge instance. This prevents one participant from sharing a reusable flag string with others. The plugin generates the flag from the challenge ID and the current user or team ID, then validates submissions by recomputing that same value on the server.

HMAC mode only accepts the generated per-participant flag. Regular CTFd flags attached to the challenge are ignored while HMAC mode is enabled.

HMAC mode injects these runtime variables:

- `CTFD_FLAG`
- `CTFD_FLAG_MODE`
- `CTFD_CHALLENGE_ID`
- `CTFD_CHALLENGE_NAME`
- `CTFD_OWNER_ID`
- `CTFD_OWNER_NAME`
- `CTFD_OWNER_MODE`

If your challenge already has a hardcoded shared flag inside the image, keep the challenge in standard flag mode and use normal CTFd flags.

If your image expects a custom environment variable such as `CTF_FLAG_CHALLENGE`, the plugin does not rename `CTFD_FLAG` automatically. In that case, update the image or entrypoint to read `CTFD_FLAG` directly, or map it to your own variable in `command` or `entrypoint`, like the compose example above.

## Player Workflow

Players open the challenge and click **Start Docker Instance for challenge**.

The plugin then:

1. Launches a dedicated instance for that player or team.
2. Publishes the required port or ports on the host.
3. Shows the connection details in the challenge modal.
4. Lets the player stop or revert the instance.

For team-mode CTFd instances, players must be on a team before launching Docker challenges.

## Admin Workflow

Use these pages during the event:

- `/admin/docker_config`
  Configure Docker access, instance defaults, and allowed repositories.
- `/admin/docker_status`
  Review active instances, lifecycle state, expiry times, and recent lifecycle actions.

The plugin records each logical instance as `creating`, `running`, `deleting`, `stopped`, or `failed`. Its background cleanup also reconciles plugin-labeled Docker containers and networks with the database, removes abandoned resources, and clears trackers for containers that exited or disappeared.
