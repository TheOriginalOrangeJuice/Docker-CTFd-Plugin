"""Tests use an isolated CTFd source tree and SQLite; never a Docker daemon."""
import importlib
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("CTFD_TEST_SOURCE", ROOT / ".test-env/CTFd")).resolve()
if not (SOURCE / "CTFd/__init__.py").is_file():
    raise RuntimeError("Provide a CTFd 3.8.8 source checkout via CTFD_TEST_SOURCE; see README test instructions")
TEST_STATE = ROOT / ".test-env/state"
TEST_STATE.mkdir(parents=True, exist_ok=True)
os.environ["SECRET_KEY"] = "isolated-plugin-tests-only"
os.environ["LOG_FOLDER"] = str(TEST_STATE / "logs")
os.environ["UPLOAD_FOLDER"] = str(TEST_STATE / "uploads")
os.environ["FLASK_SKIP_DOTENV"] = "1"
os.environ["TMPDIR"] = str(TEST_STATE)
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(ROOT.parent))
plugin = importlib.import_module(ROOT.name)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    # Prevent accidental access to either the worker or production HTTP APIs.
    def no_network(*args, **kwargs):
        pytest.fail("Tests must mock all network requests")
    monkeypatch.setattr("requests.sessions.Session.request", no_network)
    for name in tuple(os.environ):
        if name.startswith("DOCKER_CHALLENGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DOCKER_CHALLENGE_PORT_MIN", "30000")
    monkeypatch.setenv("DOCKER_CHALLENGE_PORT_MAX", "30009")
    monkeypatch.setenv("DOCKER_CHALLENGE_PUBLIC_HOST", "172.93.163.183")


@pytest.fixture
def docker_config():
    from types import SimpleNamespace
    return SimpleNamespace(
        hostname="ctf-docker.utsacyber.local:2376", tls_enabled=True,
        repositories="utsa/web,utsa/db", revert_cooldown=300,
        container_ttl=7200, max_active=0,
    )


@pytest.fixture
def app(tmp_path):
    from CTFd import create_app
    from CTFd.config import TestingConfig
    from CTFd.models import db
    from CTFd.utils import set_config
    from flask_restx import Api

    class Config(TestingConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(tmp_path / "test.db")
        SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False, "timeout": 15}}
        SERVER_NAME = "localhost.localdomain"
        LOG_FOLDER = str(tmp_path / "logs")
        UPLOAD_FOLDER = str(tmp_path / "uploads")

    app = create_app(Config)
    # Do not call plugin.load(): that would start the Docker reaper. Only attach
    # the API under test to this disposable app. Actual CTFd models/decorators run.
    api = Api(app)
    api.add_resource(plugin.ContainerAPI, "/test/container")
    api.add_resource(plugin.DockerStatus, "/test/status")
    with app.app_context():
        set_config("setup", True)
        set_config("user_mode", "users")
        set_config("ctf_name", "Isolated plugin tests")
        set_config("ctf_theme", "core")
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
