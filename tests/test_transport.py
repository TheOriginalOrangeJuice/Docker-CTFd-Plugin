from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from .conftest import ROOT, plugin
from .fake_docker import Response


@pytest.mark.parametrize("error", [None, requests.exceptions.SSLError("certificate verification failed")])
def test_mtls_verification_and_private_temporary_file_cleanup(monkeypatch, error):
    config = SimpleNamespace(hostname="ctf-docker.utsacyber.local:2376", tls_enabled=True,
                             ca_cert="test CA", client_cert="test client cert", client_key="test private key")
    paths = []
    def request(**kwargs):
        assert kwargs["url"] == "https://ctf-docker.utsacyber.local:2376/containers/json?all=1"
        assert isinstance(kwargs["verify"], str)  # CA file, never verify=False.
        paths.extend([Path(kwargs["verify"]), *map(Path, kwargs["cert"])])
        assert all(ROOT in path.parents and path.is_file() for path in paths)
        assert all(path.stat().st_mode & 0o077 == 0 for path in paths)
        assert [p.read_text() for p in paths] == ["test CA", "test client cert", "test private key"]
        if error is not None:
            raise error
        return Response([])
    monkeypatch.setattr(plugin.requests, "request", request)
    response = plugin.docker_api_request(config, "/containers/json?all=1")
    assert response.status_code == (599 if error else 200)
    assert len(paths) == 3 and not any(path.exists() for path in paths)


def test_missing_tls_credentials_never_fall_back_to_plaintext(monkeypatch):
    config = SimpleNamespace(hostname="ctf-docker.utsacyber.local:2376", tls_enabled=True,
                             ca_cert="CA", client_cert="cert", client_key=None)
    request = Mock()
    monkeypatch.setattr(plugin.requests, "request", request)
    assert plugin.docker_api_request(config, "/containers/json").status_code == 599
    request.assert_not_called()
