"""In-memory Engine API with start-time port arbitration and injectable failures."""
from copy import deepcopy
import threading
from urllib.parse import parse_qs, unquote, urlsplit


class Response:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = deepcopy(payload)
        self.status_code = status_code
        self.text = text

    def json(self):
        return deepcopy(self.payload)


class FakeDocker:
    def __init__(self):
        self.lock = threading.RLock()
        self.containers = {}
        self.networks = {}
        self.calls = []
        self.next_id = 0
        self.fail_starts = 0
        self.delete_failures = set()
        self.external_ports = set()
        self.start_barrier = None
        self.snapshot_barrier = None
        self.snapshots = 0
        self.image_id = "sha256:" + "a" * 64
        self.images = {
            "utsa/web:latest": {"Id": self.image_id, "RepoTags": ["utsa/web:latest"], "RepoDigests": ["utsa/web@sha256:" + "d" * 64], "Config": {"ExposedPorts": {"8080/tcp": {}}}},
            "utsa/db:latest": {"Id": "sha256:" + "b" * 64, "RepoTags": ["utsa/db:latest"], "Config": {}},
        }

    def request(self, docker, url, headers=None, method="GET", data=None):
        parts = urlsplit(url)
        path = parts.path
        query = parse_qs(parts.query)
        with self.lock:
            self.calls.append((method, url, deepcopy(data)))
            if method == "GET" and path.startswith("/images/") and path.endswith("/json"):
                image = unquote(path[len("/images/"):-len("/json")])
                return Response(self.images[image]) if image in self.images else Response({}, 404, "unknown image")
            if method == "GET" and path == "/containers/json":
                snapshot = [{"Id": key, "Ports": [], "Labels": c["Config"].get("Labels", {}),
                             "State": "running" if c["State"]["Running"] else "exited"}
                            for key, c in self.containers.items()]
                barrier = self.snapshot_barrier if self.snapshots < 2 else None
                self.snapshots += 1
            else:
                snapshot, barrier = None, None
        if snapshot is not None:
            if barrier is not None:
                barrier.wait(timeout=10)
            return Response(snapshot)
        if method == "POST" and path.endswith("/start") and self.start_barrier is not None:
            with self.lock:
                barrier = self.start_barrier
                # Only the first two starts meet here; retries must not wait.
                self.next_barrier_start = getattr(self, "next_barrier_start", 0) + 1
                if self.next_barrier_start >= 2:
                    self.start_barrier = None
            barrier.wait(timeout=10)
        with self.lock:
            if method == "GET" and path.startswith("/containers/") and path.endswith("/json"):
                key = unquote(path[len("/containers/"):-len("/json")])
                container = self.containers.get(key) or next((c for c in self.containers.values() if c["Name"] == key), None)
                return Response(container) if container else Response({}, 404)
            if method == "POST" and path == "/containers/create":
                name = query["name"][0]
                if any(c["Name"] == name for c in self.containers.values()):
                    return Response({}, 409, "container name is in use")
                self.next_id += 1
                key = "container-" + str(self.next_id)
                network_mode = data["HostConfig"].get("NetworkMode", "bridge")
                network = self.networks.get(network_mode) or next((n for n in self.networks.values() if n["Name"] == network_mode), None)
                if network_mode not in {"bridge", "default"} and network is None:
                    return Response({}, 404, "network not found")
                networks = {}
                if network:
                    endpoints = data.get("NetworkingConfig", {}).get("EndpointsConfig", {})
                    endpoint = endpoints.get(network_mode, {})
                    networks[network["Name"]] = {"NetworkID": network["Id"], **deepcopy(endpoint)}
                self.containers[key] = {
                    "Id": key, "Name": name, "Config": {"Labels": data.get("Labels", {})},
                    "HostConfig": deepcopy(data["HostConfig"]), "State": {"Running": False},
                    "NetworkSettings": {"Ports": {}, "Networks": networks},
                }
                return Response({"Id": key}, 201)
            if method == "POST" and path.endswith("/start"):
                key = path.split("/")[2]
                container = self.containers[key]
                requested = {int(b["HostPort"]) for bindings in container["HostConfig"].get("PortBindings", {}).values() for b in bindings}
                occupied = set(self.external_ports)
                for other_id, other in self.containers.items():
                    if other_id != key and other["State"]["Running"]:
                        occupied.update(int(b["HostPort"]) for bindings in other["HostConfig"].get("PortBindings", {}).values() for b in bindings)
                if self.fail_starts or requested & occupied:
                    self.fail_starts = max(0, self.fail_starts - 1)
                    return Response({}, 500, "Bind for 172.16.100.50:30000 failed: port is already allocated")
                container["State"]["Running"] = True
                container["NetworkSettings"]["Ports"] = deepcopy(container["HostConfig"].get("PortBindings", {}))
                return Response({}, 204)
            if method == "DELETE" and path.startswith("/containers/"):
                key = path.split("/")[2]
                if key in self.delete_failures:
                    return Response({}, 500, "injected deletion failure")
                if key not in self.containers:
                    return Response({}, 404)
                del self.containers[key]
                return Response({}, 204)
            if method == "POST" and path == "/networks/create":
                if data.get("CheckDuplicate") and any(n["Name"] == data["Name"] for n in self.networks.values()):
                    return Response({}, 409, "network name is in use")
                self.next_id += 1
                key = "network-" + str(self.next_id)
                self.networks[key] = {"Id": key, **deepcopy(data)}
                return Response({"Id": key}, 201)
            if method == "GET" and path == "/networks":
                return Response(list(self.networks.values()))
            if method == "GET" and path.startswith("/networks/"):
                network_id = unquote(path.split("/")[2])
                network = self.networks.get(network_id) or next((n for n in self.networks.values() if n["Name"] == network_id), None)
                if not network:
                    return Response({}, 404)
                endpoints = {key: {"Name": c["Name"]} for key, c in self.containers.items()
                             if c["HostConfig"].get("NetworkMode") in {network["Id"], network["Name"]}}
                return Response({**network, "Containers": endpoints})
            if method == "DELETE" and path.startswith("/networks/"):
                network_id = path.split("/")[2]
                network = self.networks.get(network_id)
                if network_id in self.delete_failures:
                    return Response({}, 500, "injected network deletion failure")
                if network and any(c["HostConfig"].get("NetworkMode") in {network_id, network["Name"]} for c in self.containers.values()):
                    return Response({}, 409, "network has active endpoints")
                if not network:
                    return Response({}, 404)
                self.networks.pop(network_id, None)
                return Response({}, 204)
        raise AssertionError(f"Unmocked Engine request: {method} {url}")

    def install(self, monkeypatch, plugin):
        monkeypatch.setattr(plugin, "do_request", self.request)
        def request_with_body(docker, url, data=None, headers=None, method="POST"):
            return self.request(docker, url, headers=headers, method=method, data=data)
        monkeypatch.setattr(plugin, "do_request_with_body", request_with_body)
        return self
