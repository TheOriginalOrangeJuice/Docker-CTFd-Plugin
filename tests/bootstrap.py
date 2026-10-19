"""Import this CTFd plugin as CTFd does: inside an application context."""
import sys

import pytest
from CTFd import create_app
from CTFd.config import TestingConfig


class BootstrapConfig(TestingConfig):
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SERVER_NAME = "localhost.localdomain"


app = create_app(BootstrapConfig)
with app.app_context():
    # Preload the package before pytest tries to collect its __init__.py.
    import docker_challenges  # noqa: F401

raise SystemExit(pytest.main(sys.argv[1:]))
