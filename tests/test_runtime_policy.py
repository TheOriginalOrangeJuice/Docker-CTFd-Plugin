from types import SimpleNamespace

import pytest
from werkzeug.exceptions import BadRequest
import yaml

from .conftest import plugin
from .fake_docker import FakeDocker
from .test_remote_runtime import SINGLE, STACK


@pytest.fixture
def engine(monkeypatch):
    return FakeDocker().install(monkeypatch, plugin)


RESTRICTED_OPTIONS = [
    {"privileged": True}, {"privileged": False},
    {"network_mode": "host"}, {"network_mode": "container:ctfd"},
    {"pid": "host"}, {"pid": "container:ctfd"}, {"ipc": "host"},
    {"ipc": "container:ctfd"}, {"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]},
    {"volumes": ["/etc:/host:ro"]},
    {"volumes": [{"type": "bind", "source": "/", "target": "/host"}]},
    {"volumes_from": ["ctfd"]}, {"devices": ["/dev/sda:/dev/sda"]},
    {"cap_add": ["SYS_ADMIN"]}, {"cap_add": ["NET_ADMIN"]}, {"cap_add": ["ALL"]},
    {"security_opt": ["seccomp=unconfined"]}, {"userns_mode": "host"},
    {"runtime": "nvidia"}, {"device_cgroup_rules": ["a *:* rwm"]},
    {"deploy": {"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}}},
    {"sysctls": {"net.ipv4.ip_forward": "1"}}, {"cgroup_parent": "/"},
    {"HostConfig": {"Privileged": True}},
    {"ports": [{"target": 80, "host_ip": "0.0.0.0"}]},
    {"ports": ["0.0.0.0:8080:80"]},
]


@pytest.mark.parametrize("options", RESTRICTED_OPTIONS)
def test_disallowed_service_runtime_options_never_reach_docker(docker_config, engine, options):
    document = {"services": {"web": {"image": SINGLE, **options}}}
    with pytest.raises(ValueError):
        plugin.create_stack(docker_config, yaml.safe_dump(document), "owner", 1, [])
    assert engine.calls == []


@pytest.mark.parametrize("field", ["volumes", "networks", "configs", "secrets", "include", "x-unsafe"])
def test_unsupported_compose_top_level_is_rejected(field):
    with pytest.raises(ValueError, match="top-level"):
        plugin.parse_compose_content(yaml.safe_dump({"services": {"web": {"image": SINGLE}}, field: {"host": {"external": True}}}))


def test_yaml_merge_cannot_hide_restricted_keys():
    content = """services:
  base: &base
    image: utsa/web:latest
    privileged: true
  web:
    <<: *base
    ports: [8080]
"""
    with pytest.raises(ValueError, match="disallowed"):
        plugin.parse_compose_content(content)


def test_single_and_compose_share_resource_security_baseline(docker_config, engine):
    single = plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    plugin.create_stack(docker_config, STACK, "owner", 2, [])
    configs = [data["HostConfig"] for _, url, data in engine.calls if url.startswith("/containers/create")]
    expected = {
        "Memory": 512_000_000, "NanoCpus": 1_000_000_000, "PidsLimit": 256,
        "Privileged": False, "PidMode": "", "IpcMode": "private",
        "SecurityOpt": ["no-new-privileges:true"], "CapDrop": ["NET_RAW"],
        "ReadonlyRootfs": False,
    }
    assert {key: single["config"]["HostConfig"][key] for key in expected} == expected
    assert len(configs) == 3
    for config in configs:
        assert {key: config[key] for key in expected} == expected
        assert "Binds" not in config and "Mounts" not in config and "Devices" not in config


def test_operator_defaults_and_explicit_service_limits(docker_config, engine, monkeypatch):
    monkeypatch.setenv("DOCKER_CHALLENGE_MEMORY_LIMIT", "1g")
    monkeypatch.setenv("DOCKER_CHALLENGE_CPU_LIMIT", "2.5")
    monkeypatch.setenv("DOCKER_CHALLENGE_PIDS_LIMIT", "128")
    single = plugin.create_container(docker_config, SINGLE, "owner", 1, [])["config"]["HostConfig"]
    document = {"services": {"web": {"image": SINGLE, "mem_limit": "256m", "cpus": 0.5, "pids_limit": 64, "cap_drop": ["ALL"], "read_only": True}}}
    plugin.create_stack(docker_config, yaml.safe_dump(document), "owner", 2, [])
    config = next(data["HostConfig"] for _, url, data in reversed(engine.calls) if url.startswith("/containers/create"))
    assert (single["Memory"], single["NanoCpus"], single["PidsLimit"]) == (1_000_000_000, 2_500_000_000, 128)
    assert (config["Memory"], config["NanoCpus"], config["PidsLimit"]) == (256_000_000, 500_000_000, 64)
    assert config["ReadonlyRootfs"] is True
    assert set(config["CapDrop"]) == {"NET_RAW", "ALL"}
    assert config["SecurityOpt"] == ["no-new-privileges:true"]


@pytest.mark.parametrize("name,value", [
    ("MEMORY_LIMIT", "garbage"), ("MEMORY_LIMIT", "0"), ("MEMORY_LIMIT", "32g"),
    ("CPU_LIMIT", "nan"), ("CPU_LIMIT", "inf"), ("CPU_LIMIT", "0"),
    ("CPU_LIMIT", "0.00000000001"), ("CPU_LIMIT", "100"),
    ("PIDS_LIMIT", "0"), ("PIDS_LIMIT", "-1"), ("PIDS_LIMIT", "NaN"), ("PIDS_LIMIT", "1.5"),
])
@pytest.mark.parametrize("stack", [False, True])
def test_invalid_operator_limits_fail_instead_of_becoming_unlimited(docker_config, engine, monkeypatch, name, value, stack):
    monkeypatch.setenv("DOCKER_CHALLENGE_" + name, value)
    with pytest.raises(ValueError):
        if stack:
            plugin.create_stack(docker_config, STACK, "owner", 1, [])
        else:
            plugin.create_container(docker_config, SINGLE, "owner", 1, [])
    assert not any(method == "POST" for method, _, _ in engine.calls)


@pytest.mark.parametrize("limits", [{"cpus": float("nan")}, {"cpus": float("inf")}, {"cpus": 0}, {"cpus": -1}, {"pids_limit": -1}, {"pids_limit": 0}, {"mem_limit": 0}, {"mem_limit": "0.0001b"}])
def test_compose_limits_cannot_disable_isolation(limits):
    with pytest.raises(ValueError):
        plugin.parse_compose_content(yaml.safe_dump({"services": {"web": {"image": SINGLE, **limits}}}))


@pytest.mark.parametrize("field", ["HostConfig", "privileged", "network_mode", "environment", "volumes", "cap_add", "image", "command", "PortBindings"])
def test_admin_top_level_runtime_overrides_rejected(app, monkeypatch, docker_config, field):
    monkeypatch.setattr(plugin, "get_docker_config", lambda: docker_config)
    with app.test_request_context(), pytest.raises(BadRequest):
        plugin.normalize_challenge_payload({"docker_image": SINGLE, field: "unapproved"})


def test_saved_images_checked_against_allowlist_in_admin_payload(app, monkeypatch, docker_config):
    monkeypatch.setattr(plugin, "get_docker_config", lambda: docker_config)
    with app.test_request_context():
        with pytest.raises(BadRequest, match="allow-list"):
            plugin.normalize_challenge_payload({"docker_image": "unapproved:latest"})
        with pytest.raises(BadRequest, match="allow-list"):
            plugin.normalize_challenge_payload({"docker_mode": "compose", "compose_content": "services: {web: {image: 'unapproved:latest'}}"})
        result = plugin.normalize_challenge_payload({"docker_mode": "compose", "compose_content": STACK})
        assert result["docker_image"] == "compose"
