from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import socket
import threading

import pytest
import yaml

from .conftest import plugin
from .fake_docker import FakeDocker, Response


SINGLE = "utsa/web:latest"
STACK = """services:
  db:
    image: utsa/db:latest
  web:
    image: utsa/web:latest
    ports: ["8080", "5353/udp"]
    depends_on: [db]
    environment:
      DATABASE_HOST: db
      SELF: "{{HOST}}:{{PORT}}"
      PEER: "{{PORT_web_5353}}"
      FLAG: cannot-override-server
"""


@pytest.fixture
def engine(monkeypatch):
    return FakeDocker().install(monkeypatch, plugin)


@pytest.mark.parametrize("bind_ip", [None, "", "172.16.100.50", "127.0.0.1", "0.0.0.0", "2001:db8::50", "::"])
@pytest.mark.parametrize("stack", [False, True])
def test_worker_host_ip_on_every_binding(docker_config, engine, monkeypatch, bind_ip, stack):
    if bind_ip is not None:
        monkeypatch.setenv("DOCKER_CHALLENGE_BIND_IP", bind_ip)
    if stack:
        plugin.create_stack(docker_config, STACK, "owner:1", 1, [], runtime_env={"FLAG": "server-flag"})
    else:
        plugin.create_container(docker_config, SINGLE, "owner:1", 1, [])
    creates = [data for method, url, data in engine.calls if url.startswith("/containers/create")]
    assert creates
    for data in creates:
        for bindings in data["HostConfig"].get("PortBindings", {}).values():
            for binding in bindings:
                assert 30000 <= int(binding["HostPort"]) <= 30009
                if bind_ip:
                    assert binding["HostIp"] == bind_ip
                else:
                    assert "HostIp" not in binding


@pytest.mark.parametrize("bind_ip", ["ctf-docker.utsacyber.local", "172.16.100.999", "172.16.100.50/24", "[::1]", "::1%eth0", "239.0.0.1", " 172.16.100.50", "172.16.100.50:30000"])
@pytest.mark.parametrize("stack", [False, True])
def test_invalid_bind_ip_rejected_before_remote_requests(docker_config, engine, monkeypatch, bind_ip, stack):
    monkeypatch.setenv("DOCKER_CHALLENGE_BIND_IP", bind_ip)
    with pytest.raises(ValueError, match="DOCKER_CHALLENGE_BIND_IP"):
        if stack:
            plugin.create_stack(docker_config, STACK, "owner", 1, [])
        else:
            plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert engine.calls == []


def test_remote_bindings_include_stopped_and_unrelated_containers(docker_config, monkeypatch):
    def request(docker, url, **kwargs):
        if url == "/containers/json?all=1":
            return Response([{"Id": "running", "Ports": [{"PublicPort": 30001, "Type": "udp"}]}, {"Id": "created", "Ports": []}, {"Id": "removed"}])
        if "removed" in url:
            return Response({}, 404)
        return Response({"HostConfig": {"PortBindings": {"80/tcp": [{"HostPort": "30002", "HostIp": "127.0.0.1"}], "53/udp": [{"HostPort": "0"}]}}, "NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "30003"}], "443/tcp": None}}})
    monkeypatch.setattr(plugin, "do_request", request)
    assert plugin.get_unavailable_ports(docker_config) == [30001, 30002, 30003]


@pytest.mark.parametrize("response", [None, Response([], 500), Response({}, 200), Response([{}]), Response([{"Id": "bad"}], 200)])
def test_remote_inspection_fails_closed(docker_config, monkeypatch, response):
    monkeypatch.setattr(plugin, "do_request", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError):
        plugin.get_unavailable_ports(docker_config)


def test_local_socket_is_irrelevant_and_remote_allocations_are_skipped(docker_config, engine, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("Local socket probing is forbidden"))
    engine.containers["other-application"] = {"Id": "other-application", "Name": "unrelated", "Config": {"Labels": {}}, "HostConfig": {"PortBindings": {"80/tcp": [{"HostPort": "30000"}]}}, "NetworkSettings": {}, "State": {"Running": False}}
    monkeypatch.setattr(plugin.random, "choice", min)
    result = plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert result["ports"] == ["30001/tcp"]


@pytest.mark.parametrize("stack", [False, True])
def test_remote_non_docker_listener_collision_retries_and_cleans_up(docker_config, engine, monkeypatch, stack):
    engine.external_ports.add(30000)  # Invisible to the remote Docker inventory.
    monkeypatch.setattr(plugin.random, "choice", min)
    if stack:
        result = plugin.create_stack(docker_config, STACK, "owner", 1, [])
        assert len(result["containers"]) == len(engine.containers) == 2
        assert len(engine.networks) == 1
        assert len([c for c in engine.calls if c[1] == "/networks/create"]) == 2
    else:
        result = plugin.create_container(docker_config, SINGLE, "owner", 1, [])
        assert result["ports"] == ["30001/tcp"]
        assert len(engine.containers) == 1
    assert any(method == "DELETE" and "v=true" in url for method, url, _ in engine.calls)


@pytest.mark.parametrize("stack", [False, True])
def test_retry_exhaustion_leaves_no_resources(docker_config, engine, monkeypatch, stack):
    engine.fail_starts = 10
    monkeypatch.setenv("DOCKER_CHALLENGE_PORT_RETRIES", "2")
    with pytest.raises(RuntimeError, match="Failed to start"):
        if stack:
            plugin.create_stack(docker_config, STACK, "owner", 1, [])
        else:
            plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert engine.containers == engine.networks == {}


def test_port_pool_exhaustion_rolls_back_partial_stack(docker_config, engine, monkeypatch):
    monkeypatch.setenv("DOCKER_CHALLENGE_PORT_MAX", "30001")
    spec = yaml.safe_load(STACK)
    spec["services"]["web"]["ports"].append("8081")
    with pytest.raises(RuntimeError, match="No free"):
        plugin.create_stack(docker_config, yaml.safe_dump(spec), "owner", 1, [])
    assert engine.containers == engine.networks == {}


@pytest.mark.parametrize("stack", [False, True])
def test_concurrent_owners_survive_same_snapshot_and_start_collision(docker_config, engine, monkeypatch, stack):
    # Both requests see the same empty inventory. Docker serializes start; the
    # loser removes only its own resources and refreshes/retries another port.
    engine.snapshot_barrier = threading.Barrier(2)
    engine.start_barrier = threading.Barrier(2)
    monkeypatch.setattr(plugin.random, "choice", min)
    def start(owner):
        if stack:
            result = plugin.create_stack(docker_config, STACK, owner, 1, [])
            return tuple(port for c in result["containers"] for port in c["ports"].split(',') if port)
        return tuple(plugin.create_container(docker_config, SINGLE, owner, 1, [])["ports"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, "owner:" + str(i)) for i in range(2)]
        ports = {f.result(timeout=20) for f in futures}
    expected = {("30000/tcp", "30001/udp"), ("30002/tcp", "30003/udp")} if stack else {("30000/tcp",), ("30001/tcp",)}
    assert ports == expected
    assert len(engine.containers) == (4 if stack else 2)
    assert len(engine.networks) == 2
    assert all(c["State"]["Running"] for c in engine.containers.values())
    assert len([c for c in engine.calls if c[1].startswith("/containers/create")]) == (6 if stack else 3)


def test_stack_keeps_internal_dns_environment_and_published_port_mapping(docker_config, engine, monkeypatch):
    monkeypatch.setattr(plugin.random, "choice", min)
    result = plugin.create_stack(docker_config, STACK, "owner", 1, [], runtime_env={"FLAG": "authoritative"})
    creates = [data for _, url, data in engine.calls if url.startswith("/containers/create")]
    assert [c["service_name"] for c in result["containers"]] == ["db", "web"]
    assert "PortBindings" not in creates[0]["HostConfig"]
    web = creates[1]
    assert web["NetworkingConfig"]["EndpointsConfig"][result["network_id"]]["Aliases"] == ["web"]
    assert "DATABASE_HOST=db" in web["Env"]
    assert "FLAG=authoritative" in web["Env"]
    assert "SELF=172.93.163.183:30000" in web["Env"]
    assert "PEER=30001" in web["Env"]
    assert result["containers"][1]["port_mappings"] == [{"target": 8080, "published": 30000, "protocol": "tcp"}, {"target": 5353, "published": 30001, "protocol": "udp"}]


def test_container_ids_are_pinned_after_inspection(docker_config, engine):
    result = plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert result["config"]["Image"] == engine.image_id
    result = plugin.create_stack(docker_config, STACK, "owner", 2, [])
    creates = [data for _, url, data in engine.calls if url.startswith("/containers/create")]
    assert all(data["Image"].startswith("sha256:") for data in creates)


@pytest.mark.parametrize("image", ["unapproved/web:latest", "utsa/web-evil:latest", "utsa/web/other:latest", "sha256:" + "a" * 64])
@pytest.mark.parametrize("stack", [False, True])
def test_allowlist_enforced_at_launch_before_remote_requests(docker_config, engine, image, stack):
    with pytest.raises(ValueError, match="allow-list"):
        if stack:
            plugin.create_stack(docker_config, "services: {web: {image: '" + image + "'}}", "owner", 1, [])
        else:
            plugin.create_container(docker_config, image, "owner", 1, [])
    assert engine.calls == []


def test_allowlist_repository_semantics_are_explicit():
    assert plugin.image_is_allowed("utsa/web:any-tag", ["utsa/web"])
    assert plugin.image_is_allowed("utsa/web@sha256:" + "a" * 64, ["utsa/web"])
    assert plugin.image_is_allowed("other/image:latest", [])  # Existing optional-list behavior.
    assert not plugin.image_is_allowed("utsa/web-extra:latest", ["utsa/web"])


def test_invalid_saved_port_selection_does_not_publish_every_image_port():
    with pytest.raises(ValueError):
        plugin.decode_published_ports(SimpleNamespace(published_ports="corrupt port selection"))


def test_retry_refreshes_worker_inventory(docker_config, engine, monkeypatch):
    monkeypatch.setattr(plugin.random, "choice", min)
    engine.fail_starts = 1
    original = engine.request
    def new_binding_during_launch(docker, url, **kwargs):
        response = original(docker, url, **kwargs)
        if url.endswith("/start") and response.status_code == 500:
            engine.containers["new-remote-reservation"] = {
                "Id": "new-remote-reservation", "Name": "unrelated", "Config": {"Labels": {}},
                "HostConfig": {"PortBindings": {"80/tcp": [{"HostPort": "30001"}]}},
                "NetworkSettings": {}, "State": {"Running": False},
            }
        return response
    monkeypatch.setattr(engine, "request", new_binding_during_launch)
    result = plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert result["ports"] == ["30002/tcp"]
    assert len([c for c in engine.calls if c[1] == "/containers/json?all=1"]) == 2


def test_image_retag_between_inspect_and_create_cannot_change_selected_image(docker_config, engine, monkeypatch):
    original = engine.request
    inspected = engine.image_id
    def retag(docker, url, **kwargs):
        response = original(docker, url, **kwargs)
        if url.startswith("/images/"):
            engine.images[SINGLE]["Id"] = "sha256:" + "f" * 64
        return response
    monkeypatch.setattr(plugin, "do_request", retag)
    result = plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert result["config"]["Image"] == inspected


def test_legacy_single_image_port_count_still_enforces_limit(docker_config, engine):
    engine.images[SINGLE]["Config"]["ExposedPorts"] = {str(p) + "/tcp": {} for p in range(8000, 8020)}
    with pytest.raises(ValueError, match="at most"):
        plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert not any(method == "POST" for method, _, _ in engine.calls)


@pytest.mark.parametrize("resource", ["container", "network"])
def test_name_conflict_cleanup_refuses_another_operation(docker_config, monkeypatch, resource):
    labels = {"ctfd.plugin": plugin.PLUGIN_LABEL, "ctfd.owner.id": "1", "ctfd.owner.mode": "users", "ctfd.challenge.id": "1", "ctfd.stack.id": "stack", "ctfd.instance.key": "instance", "ctfd.operation.token": "new-operation"}
    expected = {**labels, "ctfd.operation.token": "old-operation"}
    response = Response({"Id": "new-resource", "Labels": labels, "Config": {"Labels": labels}})
    calls = []
    def request(docker, url, method="GET", **kwargs):
        calls.append(method)
        return response
    monkeypatch.setattr(plugin, "do_request", request)
    cleanup = plugin.cleanup_conflicting_container if resource == "container" else plugin.cleanup_conflicting_network
    assert cleanup(docker_config, "resource-name", expected) is False
    assert "DELETE" not in calls


@pytest.mark.parametrize("stack", [False, True])
def test_resolved_image_must_belong_to_approved_repository(docker_config, engine, stack):
    engine.images[SINGLE]["RepoTags"] = ["unapproved/web:latest"]
    engine.images[SINGLE]["RepoDigests"] = []
    with pytest.raises(ValueError, match="approved repository"):
        if stack:
            plugin.create_stack(docker_config, STACK, "owner", 1, [])
        else:
            plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert not any(method == "POST" for method, _, _ in engine.calls)


def test_named_digest_requires_resolved_repository_evidence(docker_config, engine):
    reference = "utsa/web@sha256:" + "d" * 64
    engine.images[reference] = dict(engine.images[SINGLE])
    assert plugin.create_container(docker_config, reference, "owner", 1, [])["config"]["Image"] == engine.image_id
    engine.images[reference] = {"Id": "sha256:" + "f" * 64, "Config": {"ExposedPorts": {"8080/tcp": {}}}, "RepoDigests": ["unapproved/web@sha256:" + "d" * 64]}
    with pytest.raises(ValueError, match="approved repository"):
        plugin.create_container(docker_config, reference, "other", 1, [])
