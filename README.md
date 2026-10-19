# Docker Challenge Plugin for CTFd

Adds a Docker-backed challenge type to CTFd. Players can start an isolated instance of a challenge container or compose stack, receive a dedicated connection port, and stop or revert their own environment without affecting other competitors.

The plugin publishes challenge ports on the configured Docker worker. Players connect to `DOCKER_CHALLENGE_PUBLIC_HOST:PORT`, which can be a separate FRP gateway/VPS rather than the CTFd host.

## Screenshots

### Admin Docker Configuration

![Admin Docker configuration](docs/screenshots/admin-docker-config.png)

### Admin Challenge Creation

![Admin create Docker challenge](docs/screenshots/admin-create-docker-challenge.png)

### Player Challenge View

![Player Docker challenge modal](docs/screenshots/player-docker-challenge.png)

### Admin Instance Status

![Admin Docker status](docs/screenshots/admin-docker-status.png)


## Deployment options

- CTFd can use a separate Docker worker over verified mTLS.
- The plugin assigns worker ports; a centralized FRPC gateway can forward the whole reserved range.
- The public challenge hostname/IP is independent of the CTFd web hostname.
- A local internal socket proxy remains an alternative for existing single-host deployments.

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

## Remote worker with mTLS and centralized FRP (UTSA example)

This is a review/deployment example, not a script to apply to the existing
production Compose stack or restart its services. The patch is prepared against
plugin commit `d4be6ad24b7cbb62f910597764bdfd52a692dac9` for CTFd 3.8.8.

| Role | Endpoint |
| --- | --- |
| CTFd web/application host | `172.16.100.46` |
| Dedicated Docker worker | `172.16.100.50` |
| Verified Docker API | `ctf-docker.utsacyber.local:2376` |
| VPN/FRPC gateway | `172.16.100.38` |
| Public VPS/FRPS | `172.93.163.183` |
| Reserved published challenge ports | `30000`–`30009` |
| Worker bridge address pool | `10.80.0.0/16`, split into `/24`s |

```text
CTFd .46 -- HTTPS + client certificate --> Docker worker .50:2376
Players --> VPS/FRPS 172.93.163.183:30000-30009
             == existing authenticated, verified FRP tunnel ==>
               FRPC gateway .38 --> worker .50:30000-30009
```

The plugin creates no FRP processes, per-challenge FRPC containers, or proxies.
Forwarding is static and centralized; an unused/expired worker port should stop
accepting connections even though its FRP proxy remains registered.

### CTFd-side settings

For a future approved deployment, pass these environment values to CTFd using
your normal configuration process (this patch does not edit that stack):

```dotenv
DOCKER_CHALLENGE_BIND_IP=172.16.100.50
DOCKER_CHALLENGE_PUBLIC_HOST=172.93.163.183
DOCKER_CHALLENGE_PORT_MIN=30000
DOCKER_CHALLENGE_PORT_MAX=30009
DOCKER_CHALLENGE_MEMORY_LIMIT=512m
DOCKER_CHALLENGE_CPU_LIMIT=1.0
DOCKER_CHALLENGE_PIDS_LIMIT=256
```

In `/admin/docker_config`, set Docker Host to
`ctf-docker.utsacyber.local:2376`, **TLS Enabled: Yes**, and supply the CA
certificate, client certificate, and matching private key. The worker certificate
must have `ctf-docker.utsacyber.local` in its SANs, and that name must resolve to
`172.16.100.50` from inside CTFd. Configure a nonempty list of reviewed
repositories. Set the TTL, revert cooldown, and active-instance limit in that UI;
those stored settings take precedence over environment defaults.

The plugin uses the CA path for Requests' `verify` setting and the certificate/key
pair for client authentication. Missing/invalid TLS material fails the request;
there is no insecure/plaintext fallback. This follows Docker's
[verified TLS guidance](https://docs.docker.com/engine/security/protect-access/).
Keep the plugin database/backups and certificate material access-controlled.

### Worker requirements

Use the worker's existing verified TLS listener on 2376. Do not add an
unauthenticated TCP listener, publish the Docker API through FRP, or mount the
worker socket into a challenge. Restrict worker API access to authorized CTFd
clients, and restrict challenge-port ingress to the VPN/FRPC gateway. Verify the
policy with Docker's actual firewall forwarding rules; host firewall rules alone
may not filter Docker-published ports. Restrict challenge egress toward management
services as appropriate for the challenge designs.

The worker's reviewed `default-address-pools` configuration should include:

```json
{"default-address-pools": [{"base": "10.80.0.0/16", "size": 24}]}
```

This is a worker configuration example only. Do not overwrite existing pools or
restart Docker as part of applying the plugin patch. Verify that the pool avoids
LAN/VPN routes and existing networks. Pre-pull the reviewed images on the worker.

`DOCKER_CHALLENGE_BIND_IP` is the **worker's** interface address, not `.46` or the
FRPC gateway's `.38`. It is separate from the address shown to players. When
unset, the plugin omits `HostIp` and preserves Docker's default behavior; it does
not force a new address. IPv6 literals are supported, but their routing and Docker
bridge/host binding support need validation on the target worker.

### Centralized FRP configuration example

Retain the existing gateway's FRPC authentication and verified TLS configuration.
The example below shows a complete shape; `7000` is only an example control port,
and the CA/client paths must match your existing approved deployment. The FRPS
certificate must be valid for the selected server address (or configure its
verified DNS SAN as `transport.tls.serverName`). Do not disable verification to
work around a certificate mismatch.

For FRP versions supporting the
[port-range template](https://gofrp.org/en/docs/features/common/range/):

```toml
# Gateway 172.16.100.38: illustrative frpc.toml, not applied by this patch.
serverAddr = "172.93.163.183"
serverPort = 7000 # Use the existing FRPS control port.
auth.method = "token"
auth.token = "{{ .Envs.FRP_AUTH_TOKEN }}"
transport.tls.enable = true
transport.tls.trustedCaFile = "/etc/frp/ca.crt"
transport.tls.certFile = "/etc/frp/frpc.crt"
transport.tls.keyFile = "/etc/frp/frpc.key"

{{- range $_, $ports := parseNumberRangePair "30000-30009" "30000-30009" }}
[[proxies]]
name = "ctfd-tcp-{{ $ports.First }}"
type = "tcp"
localIP = "172.16.100.50"
localPort = {{ $ports.First }}
remotePort = {{ $ports.Second }}

[[proxies]]
name = "ctfd-udp-{{ $ports.First }}"
type = "udp"
localIP = "172.16.100.50"
localPort = {{ $ports.First }}
remotePort = {{ $ports.Second }}
{{- end }}
```

Both TCP and UDP are shown because either can be published by a challenge.
Provision only the protocols you approve, with unique proxy names. Ensure FRPS
allows this port range and that its public firewall allows the matching protocols.
Merge any `allowPorts = [{start = 30000, end = 30009}]` example into the existing
approved FRPS policy rather than replacing unrelated allowed ports. FRP's
[configuration validation](https://gofrp.org/en/docs/features/common/configure/)
and [TLS verification](https://gofrp.org/en/docs/features/common/network/network-tls/)
documentation describe the relevant checks. No FRP files/services are changed by
this patch.

### Allocation and capacity

Port availability is read from the **remote** Docker daemon, including other
applications' allocations and fixed bindings on created/stopped containers. A
local bind on CTFd is never used as evidence about the worker. Inventory errors
fail closed. Port numbers are conservatively reserved across all worker addresses
and both protocols, so this ten-number pool supports at most ten simultaneously
published bindings; a two-port stack consumes two numbers. Services without
published ports do not consume this pool.

Inventory cannot observe non-Docker listeners or make a transaction across CTFd
processes. Docker's create/start response is authoritative: collisions remove the
failed resources, refresh remote allocations, and retry another port up to
`DOCKER_CHALLENGE_PORT_RETRIES`. Do not use these ports for unrelated worker
services. Exhaustion fails the launch and leaves it retryable after cleanup.

## Image and runtime policy

A configured repository allowlist is checked on challenge save and again on every
launch, including every Compose service. Existing saved challenges do not bypass
a newly restricted list. Matches are repository names, not prefixes: allowing
`utsa/web` does not allow `utsa/web-extra` or `utsa/web/other`.

For compatibility, an **empty list permits all worker-local images**. A repository
entry permits **any tag or digest in that repository**; this is not a signed-image
or digest approval system. Use a nonempty list and reviewed immutable digest
references for production challenges. The inspected image must also have a `RepoTags` or `RepoDigests` reference in the
requested approved repository; a borrowed repository spelling cannot authorize
an unrelated image ID. After inspection the plugin launches the resolved local
image ID, so changing a tag between inspection and create cannot
substitute a different image. It does not pull images automatically.

Participant launch requests select only a saved challenge and start/stop action.
Extra runtime fields (image, Compose, environment, HostConfig, mounts, ports,
owner identity) are rejected. The legacy client `name` field is ignored as an image
selector. Single-image runtime settings are built by the server; authored
`command`, `entrypoint`, and environment overrides are accepted only through the
reviewed Compose subset. That subset intentionally allows arbitrary programs
*inside* the approved container, so challenge authors and images must be trusted.
Malformed saved published-port selections fail instead of exposing every image
port as a fallback.

## Alternative Local Docker Proxy Setup

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
- **Revert Cooldown**
  Minimum wait before the same player or team can reset the same challenge instance.
- **Instance TTL**
  Maximum lifetime for an active instance before automatic cleanup.
- **Max Active Challenges**
  Maximum number of active Docker challenge instances per player or team.
- **Allowed Repositories**
  Repository allowlist enforced when saving and launching, not just displayed in the author UI. See the image/runtime policy below.

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

### Size Docker Networks for Challenge Instances

Each running single-image instance or Compose stack uses one bridge network.
Count active **instances**, including multiple challenges per team/user. Choose
a nonoverlapping pool with spare subnets for in-progress launches and other
Docker networks. A base prefix `/B` split with `size: 24` provides up to
`2^(24-B)` networks; the example below provides up to 256 `/24` networks.

```json
{
  "default-address-pools": [
    { "base": "10.80.0.0/16", "size": 24 }
  ]
}
```

Review worker address pools as a separate approved maintenance task. This plugin
patch does not apply daemon configuration or restart Docker.

### Pre-Pull Challenge Images

Pull challenge images onto the host before the event so launches are fast and predictable.

### Global Resource and Security Defaults

Both single-image containers and every Compose service default to `512m` memory,
one CPU, and 256 PIDs. These are per-container limits, not a total stack budget.
You can change these defaults with:

```bash
DOCKER_CHALLENGE_MEMORY_LIMIT=512m
DOCKER_CHALLENGE_CPU_LIMIT=1.0
DOCKER_CHALLENGE_PIDS_LIMIT=256
```

Compose services can explicitly set positive `mem_limit`, `cpus`, and `pids_limit`
within the existing ceilings (16 GiB, 64 CPUs, 32768 PIDs). Invalid operator limits
now reject a launch rather than silently removing its limit. This changes the
previous unlimited default when memory/CPU settings were unset.

Both launch paths use private PID/IPC namespaces, `Privileged: false`,
`no-new-privileges:true`, and drop `NET_RAW`. Normal Docker capabilities other
than `NET_RAW` remain available so common database entrypoints can switch user
and initialize files. Compose `cap_drop` adds restrictions; it cannot restore
`NET_RAW` or remove no-new-privileges. Filesystems remain writable by default;
Compose can opt into `read_only: true`.

Every `cap_add` is rejected, including dangerous capabilities such as
`SYS_ADMIN`, `NET_ADMIN`, and `ALL`. Privileged mode, host/container networking or
PID/IPC sharing, devices, Docker socket mounts, host binds, `volumes_from`, custom
runtimes, and security profile overrides are rejected by the Compose subset.
Named Compose volumes remain unsupported, as before. Image-declared anonymous
volumes are allowed and removed with their container during cleanup. Services
still communicate by their aliases on their instance's private bridge network.

Review setuid/file-capability and raw-socket challenges before use: the shared
security baseline intentionally prevents those privilege transitions/raw
sockets. A separate worker, reviewed images, Docker's normal seccomp/AppArmor
profiles, and worker firewall/egress policy are still required; these settings
are not a sandbox against kernel or Docker vulnerabilities.

## Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DOCKER_CHALLENGE_PORT_MIN` | First host port available for challenge instances | `30000` |
| `DOCKER_CHALLENGE_PORT_MAX` | Last host port available for challenge instances | `60000` |
| `DOCKER_CHALLENGE_BIND_IP` | Literal IPv4/IPv6 address on the Docker worker used for every published port; invalid/scoped/multicast addresses reject launches | unset: omit `HostIp`, retaining Docker's binding default |
| `DOCKER_CHALLENGE_PUBLIC_HOST` | Player-facing hostname for connection details | request host |
| `DOCKER_CHALLENGE_HMAC_SECRET` | Secret used for per-participant HMAC flag generation | CTFd `SECRET_KEY` |
| `DOCKER_CHALLENGE_REQUEST_TIMEOUT` | Docker API timeout in seconds | `10` |
| `DOCKER_CHALLENGE_REAPER_INTERVAL` | Background cleanup interval in seconds | `60` |
| `DOCKER_CHALLENGE_REVERT_COOLDOWN` | Default revert cooldown if not set in the admin UI | `300` |
| `DOCKER_CHALLENGE_CONTAINER_TTL` | Default instance TTL if not set in the admin UI | `7200` |
| `DOCKER_CHALLENGE_MAX_ACTIVE` | Default max active instances if not set in the admin UI | `0` |
| `DOCKER_CHALLENGE_MEMORY_LIMIT` | Default per-container memory limit | `512m` |
| `DOCKER_CHALLENGE_CPU_LIMIT` | Default per-container CPU limit | `1.0` |
| `DOCKER_CHALLENGE_PIDS_LIMIT` | Default per-container PID limit | `256` |
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

Each launch also creates a dedicated bridge network. The container attaches only
to that network, rather than Docker's shared default `bridge` network.

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
      PUBLIC_BASE_URL: "http://{{HOST}}:{{PORT}}"
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
- `{{HOST}}` becomes the player-facing hostname:
  `DOCKER_CHALLENGE_PUBLIC_HOST` if you set it, otherwise the current CTFd hostname from the request.
- The player will see connection details like `{{HOST}}:12345` in the challenge view.

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
- `shm_size`

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
- `{{PORT_service}}`
- `{{PORT_service_80}}`

Examples:

- `{{HOST}}` -> `ctf.example.com`
- `{{PORT}}` -> the first published port for the current service
- `{{PORT_web}}` -> the first published port for service `web`
- `{{PORT_web_80}}` -> the published host port mapped to target port `80` in service `web`

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

### Instance network isolation and upgrades

Single-image challenges and Compose stacks both use one dedicated bridge per
team/user and challenge instance. Compose services share their instance's bridge
and retain service-name DNS aliases and dependency ordering. Network names are
unique to the creation operation; a delayed or failed launch cannot reuse or
remove the network from a newer launch. Containers attach using the returned
Docker network ID, and the existing tracker `network_id` column records it.

Networks carry `ctfd.plugin`, `ctfd.challenge.id`, `ctfd.owner.id`,
`ctfd.owner.mode`, `ctfd.instance.key`, and `ctfd.operation.token` labels (plus
`ctfd.stack.id` for Compose). Cleanup inspects labels and endpoints before
deletion. It never removes Docker's built-in `bridge`, `host`, or `none` networks,
or disconnects unknown containers to force a network removal. Stop, revert,
expiry, solve cleanup, and challenge deletion all remove the instance network
after its containers. A failed network deletion retains tracker rows for retry.
The reaper also removes labeled orphan networks after failed launches, lost API
replies, or missing containers; recent matching creation reservations are
protected for the existing ten-minute reconciliation grace period. Worker/API
outages or foreign attached endpoints can delay cleanup until a later retry or
administrator intervention. Networks without sufficient ownership labels are
left for manual review.

No new database migration, Compose challenge field, or environment variable is
needed. Existing single-image instances with no `network_id` remain on the
legacy default bridge until they are stopped/reverted/expired/solved. They are
**not retroactively isolated**. After a future approved deployment, stop and
restart or revert those instances during maintenance; new launches get dedicated
networks. Existing Compose trackers and labeled networks remain supported,
including labels predating `ctfd.instance.key` and operation tokens. Review
address-pool capacity for all instances before deployment. Rolling back to the
older plugin can leave new single-image networks until its orphan reaper runs.

The patch keeps Docker's normal bridge/NAT/address-pool defaults and published
port bindings; it does not edit worker, firewall, or FRP configuration. Docker's
[bridge isolation](https://docs.docker.com/engine/network/drivers/bridge/)
separates direct container traffic across bridges, but **published host ports
and external routes remain reachable according to existing firewall policy**.
This is not an egress firewall or isolation from the Docker host. Verify real
cross-instance private-IP connectivity is blocked, same-instance Compose DNS
works, and published ports still work through FRP in a separate staging test.


## Testing this patch without production services

Use a testing checkout named `docker_challenges`, Python 3.11, and a separate
copy of the **official CTFd 3.8.8 source** inside this checkout. The runner imports the plugin under a temporary application
context, exercises actual CTFd models/API decorators against disposable SQLite
files, and blocks outbound HTTP requests. Docker is an in-memory fake: no daemon,
production database, Redis server, Docker container, or FRP service is required.
It does not call the plugin loader or start its reaper thread.

```bash
# Run in a testing checkout, never the production plugin directory.
mkdir -p .test-env
# If not already present:
git clone --depth 1 --branch 3.8.8 https://github.com/CTFd/CTFd.git .test-env/CTFd
python3.11 -m venv .test-env/venv
.test-env/venv/bin/python -m pip install \
  -r .test-env/CTFd/requirements.txt -r requirements-test.txt
PYTHON="$PWD/.test-env/venv/bin/python" ./tests/run.sh -q
.test-env/venv/bin/python -m py_compile __init__.py tests/*.py
```

`CTFD_TEST_SOURCE` can point to another isolated 3.8.8 source copy. Test artifacts,
SQLite files, logs, and downloaded runtimes belong in `.test-env/` or `.artifacts/`,
which Git ignores. The test runner fixes its temporary-file root inside this
checkout. The bootstrap application uses an in-memory database. Compatibility
with local changes to the UTSA CTFd fork still requires staging verification.

Coverage includes TCP/UDP and IPv4/IPv6 `HostIp`, malformed bind addresses,
remote running/created/stopped bindings and inspection failures, non-Docker
listener collisions, concurrent port races in both launch modes, restricted
Compose fields, allowlist checks at save and launch, image-ID pinning, shared
limits/security defaults, mTLS verification and temporary key cleanup,
participant/team tracking, TTL and solved-instance cleanup, revert cooldown,
cleanup failure retries, per-instance network membership and ownership checks,
Compose DNS, legacy-bridge compatibility, abandoned-network reconciliation,
lost Docker replies, tracker-persistence failures, and overlapping start/stop
requests. Locked lifecycle
reads refresh the ORM identity map; operation tokens prevent old requests from
deleting or failing newer instances. A name collision with a different token is
left for reconciliation/administrator review instead of deleting that resource.

### Manual verification before a production rollout

- Review the diff against the pinned plugin commit and the local CTFd fork.
  The patch changes runtime defaults and does not include a database migration.
- In a separate staging deployment, verify the real worker's server/client
  certificate chain, DNS SAN, client authorization, interface ownership, Docker
  API compatibility, CPU/memory/PID enforcement, and supported IPv6 bindings if
  used. The automated tests do not connect to the worker.
- Verify browser create/update/start/stop/revert flows, standard and HMAC flags,
  team ownership, max-active limits, and the real reaper's scheduled execution.
  Verify local UTSA module/scheduling/access policy alongside the plugin API;
  this patch does not claim to audit that fork's complete challenge ACL model.
- Exercise a multi-service web/database challenge: service-name DNS, writable
  paths, port placeholders, and NNP/NET_RAW compatibility. Both launch modes now
  use per-instance bridges. Verify containers cannot reach other instances by
  private IP, while same-instance service DNS works. Published host ports remain
  governed by existing firewall rules. Worker firewall/egress controls must
  isolate challenge traffic from management hosts.
- Check real simultaneous launches across the deployed Gunicorn workers and
  MariaDB transactions. SQLite/fake-Docker tests do not reproduce all engine,
  kernel, cgroup, or database locking behavior. Test worker outages, exhausted
  ten-port capacity, cleanup failures, and expired/solved-instance reconciliation.
- Validate the proposed FRPC/FRPS configuration separately without replacing or
  restarting existing services. Then, during an approved staging test, verify
  TCP/UDP end-to-end connections via `172.93.163.183`, and that the same endpoints
  stop accepting challenge traffic after cleanup. Check both public and worker
  firewalls; do not forward port 2376 or 2375.
