import hashlib
import hmac
import json
import os
import random
import re
import secrets
import shlex
import socket
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import requests
import yaml
from yaml.tokens import AliasToken
from flask import Blueprint, abort, current_app, has_request_context, render_template, request
from flask_restx import Namespace, Resource
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException
from wtforms import FileField, HiddenField, RadioField, SelectMultipleField, StringField

from CTFd.api import CTFd_API_v1
from CTFd.forms import BaseForm
from CTFd.forms.fields import SubmitField
from CTFd.models import ChallengeFiles, Challenges, Fails, Flags, Hints, Solves, Tags, Teams, Users, db
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import BaseChallenge, CHALLENGE_CLASSES
from CTFd.plugins.flags import get_flag_class
from CTFd.utils.config import is_teams_mode
from CTFd.utils.dates import unix_time
from CTFd.utils.decorators import admins_only, authed_only, during_ctf_time_only, require_verified_emails
from CTFd.utils.uploads import delete_file
from CTFd.utils.user import get_current_team, get_current_user, get_ip


class DockerConfig(db.Model):
    """
	Docker Config Model. This model stores the config for docker API connections.
	"""
    id = db.Column(db.Integer, primary_key=True)
    hostname = db.Column("hostname", db.String(255), index=True)
    tls_enabled = db.Column("tls_enabled", db.Boolean, default=False, index=True)
    ca_cert = db.Column("ca_cert", db.Text)
    client_cert = db.Column("client_cert", db.Text)
    client_key = db.Column("client_key", db.Text)
    repositories = db.Column("repositories", db.Text)
    revert_cooldown = db.Column("revert_cooldown", db.Integer, nullable=True)
    container_ttl = db.Column("container_ttl", db.Integer, nullable=True)
    max_active = db.Column("max_active", db.Integer, nullable=True)
    reaper_last_run = db.Column("reaper_last_run", db.Integer, nullable=True)
    reaper_lock_until = db.Column("reaper_lock_until", db.Integer, nullable=True)


class DockerChallengeTracker(db.Model):
    """
	Docker Container Tracker. This model stores the users/teams active docker containers.
	"""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column("team_id", db.String(64), index=True)
    user_id = db.Column("user_id", db.String(64), index=True)
    docker_image = db.Column("docker_image", db.String(255), index=True)
    timestamp = db.Column("timestamp", db.Integer, index=True)
    revert_time = db.Column("revert_time", db.Integer, index=True)
    instance_id = db.Column("instance_id", db.String(128), index=True)
    ports = db.Column('ports', db.String(128), index=True)
    host = db.Column('host', db.String(128), index=True)
    challenge = db.Column('challenge', db.String(256), index=True)
    challenge_id = db.Column('challenge_id', db.Integer, nullable=True, index=True)
    service_name = db.Column('service_name', db.String(128), nullable=True, index=True)
    stack_id = db.Column('stack_id', db.String(64), nullable=True, index=True)
    network_id = db.Column('network_id', db.String(128), nullable=True)
    ports_json = db.Column('ports_json', db.Text, nullable=True)
    instance_key = db.Column('instance_key', db.String(160), nullable=True, index=True)


class DockerChallengeInstance(db.Model):
    """Logical per-owner/per-challenge lifecycle record.

    Container tracker rows remain one-per-service. This table provides the
    single row that can be locked and transitioned atomically for the whole
    challenge instance.
    """
    __table_args__ = (
        db.UniqueConstraint(
            "owner_mode",
            "owner_id",
            "challenge_id",
            name="uq_docker_instance_owner_challenge",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_mode = db.Column(db.String(16), nullable=False, index=True)
    owner_id = db.Column(db.String(64), nullable=False, index=True)
    challenge_id = db.Column(db.Integer, nullable=False, index=True)
    challenge_name = db.Column(db.String(256), nullable=False)
    instance_key = db.Column(db.String(160), nullable=False, unique=True, index=True)
    state = db.Column(db.String(16), nullable=False, default="stopped", index=True)
    operation_token = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.Integer, nullable=False, index=True)
    error = db.Column(db.Text, nullable=True)


class DockerOwnerLock(db.Model):
    """Short-lived database lock row used while reserving an instance."""
    __table_args__ = (
        db.UniqueConstraint("owner_mode", "owner_id", name="uq_docker_owner_lock"),
    )

    id = db.Column(db.Integer, primary_key=True)
    owner_mode = db.Column(db.String(16), nullable=False, index=True)
    owner_id = db.Column(db.String(64), nullable=False, index=True)
    updated_at = db.Column(db.Integer, nullable=False)


class DockerAuditLog(db.Model):
    """
    Audit log of lifecycle actions for Docker-backed challenge instances.
    """
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column("timestamp", db.Integer, index=True)
    action = db.Column("action", db.String(64), index=True)
    status = db.Column("status", db.String(32), index=True)
    actor_role = db.Column("actor_role", db.String(32), index=True)
    actor_id = db.Column("actor_id", db.String(64), nullable=True, index=True)
    actor_name = db.Column("actor_name", db.String(128), nullable=True)
    owner_mode = db.Column("owner_mode", db.String(16), nullable=True, index=True)
    owner_id = db.Column("owner_id", db.String(64), nullable=True, index=True)
    owner_name = db.Column("owner_name", db.String(128), nullable=True)
    challenge_id = db.Column("challenge_id", db.Integer, nullable=True, index=True)
    challenge_name = db.Column("challenge_name", db.String(256), nullable=True, index=True)
    docker_image = db.Column("docker_image", db.String(255), nullable=True)
    service_name = db.Column("service_name", db.String(128), nullable=True)
    instance_id = db.Column("instance_id", db.String(128), nullable=True, index=True)
    stack_id = db.Column("stack_id", db.String(64), nullable=True, index=True)
    message = db.Column("message", db.Text, nullable=True)
    ip = db.Column("ip", db.String(64), nullable=True)

class DockerConfigForm(BaseForm):
    id = HiddenField()
    hostname = StringField(
        "Docker Hostname", description="The Hostname/IP and Port of your Docker Server"
    )
    tls_enabled = RadioField('TLS Enabled?')
    ca_cert = FileField('CA Cert')
    client_cert = FileField('Client Cert')
    client_key = FileField('Client Key')
    repositories = SelectMultipleField('Repositories')
    submit = SubmitField('Submit')


DEFAULT_PORT_MIN = 30000
DEFAULT_PORT_MAX = 60000
DEFAULT_REVERT_COOLDOWN = 300
DEFAULT_CONTAINER_TTL = 7200
DEFAULT_REQUEST_TIMEOUT = 10
DEFAULT_REAPER_INTERVAL = 60
DEFAULT_MAX_SERVICES = 8
DEFAULT_MAX_PUBLISHED_PORTS = 16
DEFAULT_HMAC_TEMPLATE = "flag{{HMAC}}"
DEFAULT_PORT_RETRY_LIMIT = 5
INSTANCE_RECONCILE_GRACE = 600

INSTANCE_CREATING = "creating"
INSTANCE_RUNNING = "running"
INSTANCE_DELETING = "deleting"
INSTANCE_STOPPED = "stopped"
INSTANCE_FAILED = "failed"
ACTIVE_INSTANCE_STATES = (INSTANCE_CREATING, INSTANCE_RUNNING, INSTANCE_DELETING)

PLUGIN_LABEL = "docker_challenges"
_REAPER_THREAD_STARTED = False
_REAPER_THREAD_LOCK = threading.Lock()

COMPOSE_DISALLOWED_KEYS = {
    'build',
    'cap_add',
    'cgroup_parent',
    'container_name',
    'devices',
    'dns',
    'extra_hosts',
    'ipc',
    'network_mode',
    'pid',
    'privileged',
    'runtime',
    'security_opt',
    'sysctls',
    'tmpfs',
    'userns_mode',
    'volumes',
    'volumes_from',
}
COMPOSE_ALLOWED_KEYS = {
    'image',
    'ports',
    'environment',
    'depends_on',
    'command',
    'entrypoint',
    'cpus',
    'mem_limit',
    'cap_drop',
    'read_only',
    'pids_limit',
}
MAX_COMPOSE_BYTES = 128 * 1024
MAX_YAML_ALIASES = 64
MAX_SERVICE_MEMORY_BYTES = 16 * 1024 * 1024 * 1024
MAX_SERVICE_CPUS = 64.0
MAX_SERVICE_PIDS = 32768
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _FailedResponse:
    def __init__(self, message, status_code=599):
        self.text = message
        self.status_code = status_code

    def json(self):
        raise ValueError(self.text)


def get_plugin_setting_int(name, default, minimum=None):
    value = os.environ.get(name)
    if value in (None, ""):
        result = default
    else:
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
    if minimum is not None:
        result = max(minimum, result)
    return result


def get_plugin_setting_float(name, default, minimum=None):
    value = os.environ.get(name)
    if value in (None, ""):
        result = default
    else:
        try:
            result = float(value)
        except (TypeError, ValueError):
            result = default
    if minimum is not None:
        result = max(minimum, result)
    return result


def parse_optional_int(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid integer value '{value}'")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value '{value}'") from exc


def get_config_int_value(docker, attr_name, env_name, default, minimum=None):
    configured = None
    if docker is None:
        docker = get_docker_config()
    if docker is not None:
        try:
            configured = parse_optional_int(getattr(docker, attr_name, None))
        except (TypeError, ValueError):
            configured = None
    if configured is None:
        configured = get_plugin_setting_int(env_name, default, minimum=minimum)
    elif minimum is not None:
        configured = max(minimum, configured)
    return configured


def get_request_timeout():
    return get_plugin_setting_int("DOCKER_CHALLENGE_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT, minimum=1)


def get_revert_cooldown(docker=None):
    return get_config_int_value(
        docker,
        "revert_cooldown",
        "DOCKER_CHALLENGE_REVERT_COOLDOWN",
        DEFAULT_REVERT_COOLDOWN,
        minimum=0,
    )


def get_port_retry_limit():
    return get_plugin_setting_int("DOCKER_CHALLENGE_PORT_RETRIES", DEFAULT_PORT_RETRY_LIMIT, minimum=1)


def get_container_ttl(docker=None):
    return get_config_int_value(
        docker,
        "container_ttl",
        "DOCKER_CHALLENGE_CONTAINER_TTL",
        DEFAULT_CONTAINER_TTL,
        minimum=60,
    )


def get_max_active_challenges(docker=None):
    return get_config_int_value(docker, "max_active", "DOCKER_CHALLENGE_MAX_ACTIVE", 0, minimum=0)


def get_port_bounds():
    port_min = min(get_plugin_setting_int("DOCKER_CHALLENGE_PORT_MIN", DEFAULT_PORT_MIN, minimum=1024), 65534)
    port_max = min(
        get_plugin_setting_int("DOCKER_CHALLENGE_PORT_MAX", DEFAULT_PORT_MAX, minimum=port_min + 1),
        65535,
    )
    return port_min, port_max


def parse_memory_value(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("Memory limits must be numeric")
    if isinstance(value, (int, float)):
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("Memory limits must be greater than zero")
        return parsed

    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgtp]?i?b?)?\s*", str(value), flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid memory limit '{value}'")

    amount = float(match.group(1))
    if amount <= 0:
        raise ValueError("Memory limits must be greater than zero")
    unit = (match.group(2) or "b").lower()
    multipliers = {
        "b": 1,
        "k": 1000,
        "kb": 1000,
        "ki": 1024,
        "kib": 1024,
        "m": 1000 ** 2,
        "mb": 1000 ** 2,
        "mi": 1024 ** 2,
        "mib": 1024 ** 2,
        "g": 1000 ** 3,
        "gb": 1000 ** 3,
        "gi": 1024 ** 3,
        "gib": 1024 ** 3,
        "t": 1000 ** 4,
        "tb": 1000 ** 4,
        "ti": 1024 ** 4,
        "tib": 1024 ** 4,
        "p": 1000 ** 5,
        "pb": 1000 ** 5,
        "pi": 1024 ** 5,
        "pib": 1024 ** 5,
    }
    return int(amount * multipliers[unit])


def get_default_memory_limit():
    try:
        return parse_memory_value(os.environ.get("DOCKER_CHALLENGE_MEMORY_LIMIT"))
    except ValueError:
        return None


def parse_cpu_value(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("CPU limits must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid CPU limit '{value}'")
    if parsed <= 0:
        raise ValueError("CPU limits must be greater than zero")
    return parsed


def get_default_cpu_limit():
    value = os.environ.get("DOCKER_CHALLENGE_CPU_LIMIT")
    if value in (None, ""):
        return None
    parsed = get_plugin_setting_float("DOCKER_CHALLENGE_CPU_LIMIT", 0.0, minimum=0.0)
    return parsed or None


def get_reaper_interval():
    return get_plugin_setting_int("DOCKER_CHALLENGE_REAPER_INTERVAL", DEFAULT_REAPER_INTERVAL, minimum=15)


def get_max_services_per_stack():
    return get_plugin_setting_int("DOCKER_CHALLENGE_MAX_SERVICES", DEFAULT_MAX_SERVICES, minimum=1)


def get_max_published_ports():
    return get_plugin_setting_int(
        "DOCKER_CHALLENGE_MAX_PUBLISHED_PORTS",
        DEFAULT_MAX_PUBLISHED_PORTS,
        minimum=1,
    )


def normalize_repo_selection(repositories):
    if not repositories:
        return []
    if isinstance(repositories, str):
        return [repo.strip() for repo in repositories.split(",") if repo.strip()]
    return [str(repo).strip() for repo in repositories if str(repo).strip()]


def get_repository_name(image):
    repo_part = str(image).rsplit("@", 1)[0]
    if "/" in repo_part:
        last_segment = repo_part.rsplit("/", 1)[-1]
    else:
        last_segment = repo_part
    if ":" in last_segment:
        return repo_part.rsplit(":", 1)[0]
    return repo_part


def image_is_allowed(image, allowed_repositories):
    if not allowed_repositories:
        return True
    return get_repository_name(image) in allowed_repositories


def decode_upload(file_storage):
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None
    raw = file_storage.stream.read()
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="ignore")


def get_request_data():
    return request.get_json(silent=True) or request.form


def normalize_docker_hostname(value):
    hostname = (value or "").strip()
    if not hostname:
        return ""
    if "://" in hostname:
        raise ValueError("Enter the Docker host as host:port only, without http:// or https://")
    if "/" in hostname or "?" in hostname or "#" in hostname:
        raise ValueError("Docker host must be a bare host:port value")
    if ":" not in hostname:
        raise ValueError("Docker host must include a port, for example dockerproxy:2375")
    return hostname


def extract_host_only(value):
    if not value:
        return ""
    hostname = str(value).split(",", 1)[0].strip()
    if hostname.startswith("[") and "]:" in hostname:
        return hostname.split("]:", 1)[0] + "]"
    if hostname.count(":") == 1:
        return hostname.rsplit(":", 1)[0]
    return hostname


def normalize_public_hostname(value):
    hostname = (value or "").strip()
    if not hostname:
        return ""
    if "://" in hostname:
        raise ValueError("Public host must be a bare hostname or IP without http:// or https://")
    if "/" in hostname or "?" in hostname or "#" in hostname:
        raise ValueError("Public host must not include paths, query strings, or fragments")
    if hostname.startswith("["):
        if "]:" in hostname:
            raise ValueError("Public host must not include a port. Enter only the host portion.")
        if not hostname.endswith("]"):
            raise ValueError("Bracketed IPv6 public hosts must end with ]")
        return hostname
    if hostname.count(":") == 1:
        raise ValueError("Public host must not include a port. Enter only the host portion.")
    if hostname.count(":") > 1:
        raise ValueError("IPv6 public hosts must be entered in bracketed form, for example [2001:db8::10]")
    return hostname


def get_connection_host_value(docker=None):
    configured = os.environ.get("DOCKER_CHALLENGE_PUBLIC_HOST", "")
    if configured:
        try:
            return normalize_public_hostname(configured)
        except ValueError:
            return extract_host_only(configured)
    if has_request_context():
        request_host = extract_host_only(request.headers.get("X-Forwarded-Host") or request.host)
        if request_host:
            return request_host
    if docker and docker.hostname:
        return extract_host_only(docker.hostname)
    return ""


def build_owner_filter(participant):
    owner_field = DockerChallengeTracker.team_id if is_teams_mode() else DockerChallengeTracker.user_id
    return owner_field == str(participant.id)


def get_participant(required=False):
    if is_teams_mode():
        participant = get_current_team()
        if participant is None and required:
            abort(403, "Join a team before launching Docker challenge instances")
        return participant

    participant = get_current_user()
    if participant is None and required:
        abort(403, "Authenticate before launching Docker challenge instances")
    return participant


def get_participant_name(participant):
    if participant is None:
        return None
    return getattr(participant, "name", None) or getattr(participant, "email", None) or str(participant.id)


def get_owner_kwargs(participant):
    return {
        "team_id": str(participant.id) if is_teams_mode() else None,
        "user_id": str(participant.id) if not is_teams_mode() else None,
    }


def get_owner_mode():
    return "teams" if is_teams_mode() else "users"


def get_instance_key(participant=None, challenge=None, owner_mode=None, owner_id=None, challenge_id=None):
    if participant is not None:
        owner_mode = get_owner_mode()
        owner_id = participant.id
    if challenge is not None:
        challenge_id = challenge.id
    if owner_mode in ("team", "teams"):
        owner_mode = "teams"
    elif owner_mode in ("user", "users"):
        owner_mode = "users"
    if owner_mode not in {"teams", "users"} or owner_id in (None, "") or challenge_id in (None, ""):
        return None
    return f"{owner_mode}:{owner_id}:{challenge_id}"


def get_instance_record(participant, challenge, for_update=False):
    return get_instance_record_by_identity(
        get_owner_mode(),
        participant.id,
        challenge.id,
        for_update=for_update,
    )


def get_instance_record_by_identity(owner_mode, owner_id, challenge_id, for_update=False):
    query = DockerChallengeInstance.query.filter_by(
        owner_mode="teams" if owner_mode in {"team", "teams"} else "users",
        owner_id=str(owner_id),
        challenge_id=challenge_id,
    )
    if for_update:
        query = query.with_for_update()
    return query.first()


def acquire_owner_lock(participant):
    return acquire_owner_lock_by_identity(get_owner_mode(), participant.id)


def acquire_owner_lock_by_identity(owner_mode, owner_id):
    """Acquire a short DB row lock for lifecycle reservation checks."""
    owner_mode = "teams" if owner_mode in {"team", "teams"} else "users"
    owner_id = str(owner_id)
    now = unix_time(datetime.utcnow())
    lock_row = DockerOwnerLock.query.filter_by(owner_mode=owner_mode, owner_id=owner_id).first()
    if lock_row is None:
        try:
            db.session.add(DockerOwnerLock(owner_mode=owner_mode, owner_id=owner_id, updated_at=now))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()

    lock_row = DockerOwnerLock.query.filter_by(
        owner_mode=owner_mode,
        owner_id=owner_id,
    ).with_for_update().first()
    if lock_row is None:
        raise RuntimeError("Unable to acquire the Docker instance owner lock")

    # The write makes SQLite acquire its database write lock as well. MySQL and
    # PostgreSQL retain the row lock until the reservation transaction commits.
    # Always change the value so SQLite emits an UPDATE even when two
    # lifecycle requests arrive in the same second.
    lock_row.updated_at = max(now, int(lock_row.updated_at or 0) + 1)
    db.session.flush()
    return lock_row


def update_instance_state(record, state, error=None, operation_token=None, commit=True):
    if record is None:
        return
    record.state = state
    record.updated_at = unix_time(datetime.utcnow())
    record.error = str(error)[:2000] if error else None
    if operation_token is not None:
        record.operation_token = operation_token
    if commit:
        db.session.commit()


def compare_and_set_instance_state(record, state, error=None, commit=True):
    """Update a state row only if another lifecycle operation has not changed it."""
    if record is None:
        return False
    expected_state = record.state
    expected_token = record.operation_token
    expected_updated_at = record.updated_at
    now = unix_time(datetime.utcnow())
    updated = DockerChallengeInstance.query.filter_by(
        id=record.id,
        state=expected_state,
        operation_token=expected_token,
        updated_at=expected_updated_at,
    ).update(
        {
            "state": state,
            "updated_at": now,
            "error": str(error)[:2000] if error else None,
        },
        synchronize_session=False,
    )
    if updated == 1:
        record.state = state
        record.updated_at = now
        record.error = str(error)[:2000] if error else None
    if commit:
        db.session.commit()
    return updated == 1


def ensure_legacy_instance_record(participant, challenge, trackers):
    """Create the lifecycle row for tracker data written by older versions."""
    record = get_instance_record(participant, challenge, for_update=True)
    if record is not None or not trackers:
        return record
    now = unix_time(datetime.utcnow())
    record = DockerChallengeInstance(
        owner_mode=get_owner_mode(),
        owner_id=str(participant.id),
        challenge_id=challenge.id,
        challenge_name=challenge.name,
        instance_key=get_instance_key(participant=participant, challenge=challenge),
        state=INSTANCE_RUNNING,
        operation_token=None,
        created_at=min(int(t.timestamp or now) for t in trackers),
        updated_at=now,
    )
    db.session.add(record)
    db.session.flush()
    for tracker in trackers:
        tracker.instance_key = record.instance_key
    return record


def get_record_from_tracker(tracker):
    if tracker is None:
        return None
    owner_mode = "teams" if tracker.team_id else "users" if tracker.user_id else None
    owner_id = tracker.team_id or tracker.user_id
    if tracker.challenge_id is None or owner_mode is None or owner_id is None:
        return None
    return DockerChallengeInstance.query.filter_by(
        owner_mode=owner_mode,
        owner_id=str(owner_id),
        challenge_id=tracker.challenge_id,
    ).first()


def encode_ports(ports):
    port_list = [str(port) for port in (ports or []) if str(port)]
    return ",".join(port_list), json.dumps(port_list)


def decode_ports(tracker):
    if tracker is None:
        return []
    if tracker.ports_json:
        try:
            return [str(port) for port in json.loads(tracker.ports_json)]
        except (TypeError, ValueError):
            pass
    if tracker.ports:
        return [port for port in tracker.ports.split(",") if port]
    return []


def get_runtime_owner_data(participant):
    owner_mode = get_owner_mode()
    return {
        "owner_mode": owner_mode,
        "owner_id": str(participant.id),
        "owner_name": get_participant_name(participant),
    }


def get_current_request_ip():
    if not has_request_context():
        return None
    try:
        return get_ip(req=request)
    except TypeError:
        return get_ip(request)


def get_flag_template(challenge):
    template = (getattr(challenge, "flag_template", None) or DEFAULT_HMAC_TEMPLATE).strip()
    if "{{HMAC}}" not in template:
        raise ValueError("Dynamic flag templates must include the {{HMAC}} placeholder")
    return template


def generate_hmac_flag(challenge, participant):
    secret = os.environ.get("DOCKER_CHALLENGE_HMAC_SECRET") or current_app.config.get("SECRET_KEY")
    if not secret:
        raise RuntimeError("No secret key is available for HMAC flag generation")

    template = get_flag_template(challenge)
    digest_source = f"{challenge.id}:{get_owner_mode()}:{participant.id}".encode("utf-8")
    digest = hmac.new(str(secret).encode("utf-8"), digest_source, hashlib.sha256).hexdigest()
    return template.replace("{{HMAC}}", digest)


def build_runtime_env(challenge, participant):
    env = {
        "CTFD_CHALLENGE_ID": str(challenge.id),
        "CTFD_CHALLENGE_NAME": str(challenge.name),
        "CTFD_OWNER_ID": str(participant.id),
        "CTFD_OWNER_NAME": get_participant_name(participant),
        "CTFD_OWNER_MODE": "team" if is_teams_mode() else "user",
        "CTFD_FLAG_MODE": getattr(challenge, "flag_mode", "static") or "static",
    }
    if env["CTFD_FLAG_MODE"] == "hmac":
        env["CTFD_FLAG"] = generate_hmac_flag(challenge, participant)
    return env


def get_trackers_for_challenge(participant, challenge):
    query = DockerChallengeTracker.query.filter(build_owner_filter(participant))
    if challenge.id is not None:
        query = query.filter(
            (DockerChallengeTracker.challenge_id == challenge.id) |
            ((DockerChallengeTracker.challenge_id.is_(None)) & (DockerChallengeTracker.challenge == challenge.name))
        )
    else:
        query = query.filter(DockerChallengeTracker.challenge == challenge.name)
    return query.all()


def get_distinct_active_challenge_ids(participant):
    trackers = DockerChallengeTracker.query.filter(build_owner_filter(participant)).all()
    distinct = set()
    for tracker in trackers:
        if tracker.challenge_id is not None:
            distinct.add(f"id:{tracker.challenge_id}")
        else:
            distinct.add(f"name:{tracker.challenge}")
    return distinct


def log_audit_event(
    action,
    status="success",
    challenge=None,
    tracker=None,
    participant=None,
    actor_role=None,
    actor_id=None,
    actor_name=None,
    message=None,
    ip_address=None,
):
    try:
        owner_data = get_runtime_owner_data(participant) if participant is not None else {
            "owner_mode": "teams" if tracker and tracker.team_id else "users" if tracker and tracker.user_id else None,
            "owner_id": tracker.team_id if tracker and tracker.team_id else tracker.user_id if tracker else None,
            "owner_name": None,
        }
        if owner_data["owner_name"] is None and tracker is not None:
            if tracker.team_id:
                team = Teams.query.filter_by(id=tracker.team_id).first()
                owner_data["owner_name"] = team.name if team else f"Team {tracker.team_id}"
            elif tracker.user_id:
                user = Users.query.filter_by(id=tracker.user_id).first()
                owner_data["owner_name"] = user.name if user else f"User {tracker.user_id}"

        challenge_id = getattr(challenge, "id", None) if challenge is not None else getattr(tracker, "challenge_id", None)
        challenge_name = getattr(challenge, "name", None) if challenge is not None else getattr(tracker, "challenge", None)

        entry = DockerAuditLog(
            timestamp=unix_time(datetime.utcnow()),
            action=action,
            status=status,
            actor_role=actor_role or "system",
            actor_id=str(actor_id) if actor_id not in (None, "") else None,
            actor_name=actor_name,
            owner_mode=owner_data["owner_mode"],
            owner_id=owner_data["owner_id"],
            owner_name=owner_data["owner_name"],
            challenge_id=challenge_id,
            challenge_name=challenge_name,
            docker_image=getattr(tracker, "docker_image", None),
            service_name=getattr(tracker, "service_name", None),
            instance_id=getattr(tracker, "instance_id", None),
            stack_id=getattr(tracker, "stack_id", None),
            message=message,
            ip=ip_address,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        traceback.print_exc()
        db.session.rollback()


def cleanup_expired_trackers(docker, participant):
    if not docker:
        return
    ttl = get_container_ttl(docker)
    if ttl <= 0:
        return

    now = unix_time(datetime.utcnow())
    trackers = DockerChallengeTracker.query.filter(build_owner_filter(participant)).all()
    expired = []
    for tracker in trackers:
        try:
            if tracker.timestamp is None or (now - int(tracker.timestamp)) >= ttl:
                expired.append(tracker)
        except (TypeError, ValueError):
            expired.append(tracker)
    if expired:
        delete_trackers(docker, expired, reason="expire", participant=participant, actor_role="system")


def cleanup_all_expired_trackers(docker):
    if not docker:
        return
    ttl = get_container_ttl(docker)
    if ttl <= 0:
        return

    now = unix_time(datetime.utcnow())
    trackers = DockerChallengeTracker.query.all()
    expired = []
    for tracker in trackers:
        try:
            if tracker.timestamp is None or (now - int(tracker.timestamp)) >= ttl:
                expired.append(tracker)
        except (TypeError, ValueError):
            expired.append(tracker)
    if expired:
        delete_trackers(docker, expired, reason="expire", actor_role="system")


def get_docker_config():
    return DockerConfig.query.filter_by(id=1).first() or DockerConfig.query.first()


def run_reaper_cycle():
    docker = get_docker_config()
    if not docker:
        return False

    now = unix_time(datetime.utcnow())
    lock_until = docker.reaper_lock_until or 0
    if lock_until and lock_until > now:
        return False

    lease_seconds = max(get_reaper_interval() * 10, 900)
    updated = DockerConfig.query.filter(
        DockerConfig.id == docker.id,
        (DockerConfig.reaper_lock_until.is_(None)) | (DockerConfig.reaper_lock_until < now),
    ).update({"reaper_lock_until": now + lease_seconds}, synchronize_session=False)
    db.session.commit()
    if updated != 1:
        return False

    try:
        cleanup_all_expired_trackers(docker)
        reconcile_docker_resources(docker)
        DockerConfig.query.filter_by(id=docker.id).update(
            {
                "reaper_last_run": now,
                "reaper_lock_until": now - 1,
            },
            synchronize_session=False,
        )
        db.session.commit()
        return True
    except Exception:
        traceback.print_exc()
        db.session.rollback()
        DockerConfig.query.filter_by(id=docker.id).update(
            {"reaper_lock_until": now - 1},
            synchronize_session=False,
        )
        db.session.commit()
        return False


def _reaper_loop(app):
    while True:
        time.sleep(get_reaper_interval())
        with app.app_context():
            run_reaper_cycle()


def start_reaper_thread(app):
    global _REAPER_THREAD_STARTED
    with _REAPER_THREAD_LOCK:
        if _REAPER_THREAD_STARTED:
            return
        thread = threading.Thread(target=_reaper_loop, args=(app,), daemon=True, name="docker-challenge-reaper")
        thread.start()
        _REAPER_THREAD_STARTED = True


def define_docker_admin(app):
    admin_docker_config = Blueprint('admin_docker_config', __name__, template_folder='templates',
                                    static_folder='assets')

    @admin_docker_config.route("/admin/docker_config", methods=["GET", "POST"])
    @admins_only
    def docker_config():
        docker = get_docker_config()
        form = DockerConfigForm()
        errors = []
        if request.method == "POST":
            b = docker or DockerConfig(id=1)

            ca_cert = decode_upload(request.files.get('ca_cert'))
            client_cert = decode_upload(request.files.get('client_cert'))
            client_key = decode_upload(request.files.get('client_key'))

            if ca_cert:
                b.ca_cert = ca_cert
            if client_cert:
                b.client_cert = client_cert
            if client_key:
                b.client_key = client_key

            hostname_value = request.form.get('hostname', '')
            try:
                b.hostname = normalize_docker_hostname(hostname_value)
            except ValueError as exc:
                errors.append(str(exc))
            b.tls_enabled = request.form.get('tls_enabled') == "True"
            if not b.tls_enabled:
                b.ca_cert = None
                b.client_cert = None
                b.client_key = None

            try:
                b.revert_cooldown = get_config_int_value(
                    type("ConfigValue", (), {"revert_cooldown": request.form.get("revert_cooldown")})(),
                    "revert_cooldown",
                    "DOCKER_CHALLENGE_REVERT_COOLDOWN",
                    DEFAULT_REVERT_COOLDOWN,
                    minimum=0,
                )
                b.container_ttl = get_config_int_value(
                    type("ConfigValue", (), {"container_ttl": request.form.get("container_ttl")})(),
                    "container_ttl",
                    "DOCKER_CHALLENGE_CONTAINER_TTL",
                    DEFAULT_CONTAINER_TTL,
                    minimum=60,
                )
                b.max_active = get_config_int_value(
                    type("ConfigValue", (), {"max_active": request.form.get("max_active")})(),
                    "max_active",
                    "DOCKER_CHALLENGE_MAX_ACTIVE",
                    0,
                    minimum=0,
                )
            except (TypeError, ValueError):
                errors.append("Cooldown, TTL, and max active must be valid integers.")

            selected_repositories = normalize_repo_selection(request.form.to_dict(flat=False).get('repositories', []))
            b.repositories = ','.join(selected_repositories) if selected_repositories else None
            if not b.hostname:
                errors.append("Docker hostname is required.")
            if b.tls_enabled and not all([b.ca_cert, b.client_cert, b.client_key]):
                errors.append("TLS is enabled, but one or more certificate files are missing.")

            if not errors:
                db.session.add(b)
                db.session.commit()
                docker = get_docker_config()

        repos = get_repositories(docker)
        if len(repos) == 0:
            form.repositories.choices = [("ERROR", "Failed to Connect to Docker")]
        else:
            form.repositories.choices = [(d, d) for d in repos]

        dconfig = get_docker_config()
        selected_repos = normalize_repo_selection(dconfig.repositories if dconfig else None)
        return render_template("docker_config.html", config=dconfig, form=form, repos=selected_repos, errors=errors)

    app.register_blueprint(admin_docker_config)


def define_docker_status(app):
    admin_docker_status = Blueprint('admin_docker_status', __name__, template_folder='templates',
                                    static_folder='assets')

    @admin_docker_status.route("/admin/docker_status", methods=["GET", "POST"])
    @admins_only
    def docker_admin():
        docker_config = get_docker_config()
        cleanup_all_expired_trackers(docker_config)

        docker_tracker = DockerChallengeTracker.query.order_by(DockerChallengeTracker.timestamp.desc()).all()
        dockers = []
        for tracker in docker_tracker:
            instance_record = get_record_from_tracker(tracker)
            display_name = ""
            if tracker.team_id:
                team = Teams.query.filter_by(id=tracker.team_id).first()
                display_name = team.name if team else f"Team {tracker.team_id}"
            elif tracker.user_id:
                user = Users.query.filter_by(id=tracker.user_id).first()
                display_name = user.name if user else f"User {tracker.user_id}"

            dockers.append({
                'id': tracker.id,
                'participant': display_name,
                'docker_image': tracker.docker_image,
                'challenge': tracker.challenge,
                'challenge_id': tracker.challenge_id,
                'service_name': tracker.service_name,
                'instance_id': tracker.instance_id,
                'timestamp': tracker.timestamp,
                'revert_time': tracker.revert_time,
                'expires_at': tracker.timestamp + get_container_ttl(docker_config),
                'ports': decode_ports(tracker),
                'stack_id': tracker.stack_id,
                'state': instance_record.state if instance_record else INSTANCE_RUNNING,
            })
        audit_logs = DockerAuditLog.query.order_by(DockerAuditLog.timestamp.desc()).limit(100).all()
        settings_summary = {
            'revert_cooldown': get_revert_cooldown(docker_config),
            'container_ttl': get_container_ttl(docker_config),
            'max_active': get_max_active_challenges(docker_config),
            'port_min': get_port_bounds()[0],
            'port_max': get_port_bounds()[1],
            'reaper_interval': get_reaper_interval(),
            'reaper_last_run': docker_config.reaper_last_run if docker_config else None,
        }
        return render_template(
            "admin_docker_status.html",
            dockers=dockers,
            team_mode=is_teams_mode(),
            audit_logs=audit_logs,
            settings_summary=settings_summary,
        )

    app.register_blueprint(admin_docker_status)


kill_container = Namespace("nuke", description='Endpoint to nuke containers')


@kill_container.route("", methods=['POST'])
class KillContainerAPI(Resource):
    @admins_only
    def post(self):
        payload = request.get_json(silent=True) or request.args
        container = payload.get('container')
        full = str(payload.get('all', '')).lower()
        docker_config = get_docker_config()
        if not docker_config:
            return {"success": False, "message": "Docker is not configured"}, 503
        docker_tracker = DockerChallengeTracker.query.all()
        actor = get_current_user()
        if full == "true":
            failed = delete_trackers(
                docker_config,
                docker_tracker,
                reason="admin_nuke",
                actor_role="admin",
                actor_id=actor.id if actor else None,
                actor_name=getattr(actor, "name", None),
            )
            if failed:
                return {"success": False, "message": "One or more instances could not be removed"}, 500

        elif container != 'null' and container in [c.instance_id for c in docker_tracker]:
            entry = DockerChallengeTracker.query.filter_by(instance_id=container).first()
            failed = delete_trackers(
                docker_config,
                [entry] if entry else [],
                reason="admin_nuke",
                actor_role="admin",
                actor_id=actor.id if actor else None,
                actor_name=getattr(actor, "name", None),
            )
            if failed:
                return {"success": False, "message": "Failed to remove the requested instance"}, 500
        else:
            return {"success": False}, 404
        return {"success": True}


def get_client_cert(docker):
    if not docker or not docker.ca_cert or not docker.client_cert or not docker.client_key:
        raise ValueError("TLS is enabled but one or more certificate files are missing")

    temp_paths = []

    def _write_temp(contents):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(contents.encode("utf-8"))
        handle.flush()
        handle.close()
        temp_paths.append(handle.name)
        return handle.name

    ca_path = _write_temp(docker.ca_cert)
    client_path = _write_temp(docker.client_cert)
    key_path = _write_temp(docker.client_key)
    return (client_path, key_path), ca_path, temp_paths


def docker_api_request(docker, url, headers=None, method='GET', data=None):
    if not docker or not docker.hostname:
        return _FailedResponse("Docker is not configured")

    headers = headers or {}
    prefix = 'https' if docker.tls_enabled else 'http'
    request_url = f"{prefix}://{docker.hostname}{url}"
    temp_paths = []
    cert = None
    verify = True

    if data is not None and not isinstance(data, (str, bytes)):
        body = json.dumps(data)
        headers.setdefault('Content-Type', 'application/json')
    else:
        body = data

    try:
        if docker.tls_enabled:
            cert, verify, temp_paths = get_client_cert(docker)
        return requests.request(
            method=method,
            url=request_url,
            data=body,
            headers=headers,
            cert=cert,
            verify=verify,
            timeout=get_request_timeout(),
        )
    except (requests.RequestException, ValueError) as exc:
        traceback.print_exc()
        return _FailedResponse(f"{type(exc).__name__}: {exc}")
    finally:
        for file_path in temp_paths:
            Path(file_path).unlink(missing_ok=True)


def do_request(docker, url, headers=None, method='GET'):
    return docker_api_request(docker, url=url, headers=headers, method=method)


def do_request_with_body(docker, url, data=None, headers=None, method='POST'):
    return docker_api_request(docker, url=url, headers=headers, method=method, data=data)


def parse_response_json(response, default=None):
    if response is None:
        return default
    try:
        return response.json()
    except ValueError:
        return default


def get_repositories(docker, tags=False, repos=None):
    response = do_request(docker, '/images/json?all=1')
    images = parse_response_json(response, default=[]) or []
    allowed_repositories = set(normalize_repo_selection(repos))
    results = set()

    for image in images:
        for tag in image.get('RepoTags') or []:
            repository_name = get_repository_name(tag)
            if repository_name == '<none>':
                continue
            if allowed_repositories and repository_name not in allowed_repositories:
                continue
            results.add(tag if tags else repository_name)

    return sorted(results)


def get_unavailable_ports(docker):
    response = do_request(docker, '/containers/json?all=1')
    containers = parse_response_json(response, default=[]) or []
    result = []
    for container in containers:
        for port in container.get('Ports') or []:
            public_port = port.get('PublicPort')
            if public_port is not None:
                result.append(int(public_port))
    return result


def get_required_ports(docker, image):
    response = do_request(docker, f"/images/{quote(image, safe='')}/json")
    if response is None:
        raise RuntimeError(f"Failed to inspect image '{image}'")
    if response.status_code >= 400:
        raise RuntimeError(f"Image '{image}' could not be inspected: {response.text}")

    exposed = (parse_response_json(response, default={}) or {}).get('Config', {}).get('ExposedPorts', {})
    return sorted(exposed.keys()) if exposed else []


def normalize_published_ports(value):
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            values = []
        elif raw.startswith('['):
            try:
                values = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Published ports must be a list") from exc
        else:
            values = [item.strip() for item in raw.split(',') if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        raise ValueError("Published ports must be a list")

    result = []
    seen = set()
    for item in values:
        match = re.fullmatch(r"\s*(\d{1,5})(?:/(tcp|udp))?\s*", str(item), flags=re.IGNORECASE)
        if not match:
            raise ValueError(f"Invalid published container port '{item}'")
        port = int(match.group(1))
        if not (1 <= port <= 65535):
            raise ValueError(f"Published container port '{item}' is out of range")
        normalized = f"{port}/{(match.group(2) or 'tcp').lower()}"
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def decode_published_ports(challenge):
    raw = getattr(challenge, 'published_ports', None)
    if raw in (None, ''):
        return None
    try:
        return normalize_published_ports(raw)
    except ValueError:
        return None


def is_host_port_available(port, protocol='tcp'):
    socket_type = socket.SOCK_DGRAM if str(protocol).lower() == 'udp' else socket.SOCK_STREAM
    test_socket = socket.socket(socket.AF_INET, socket_type)
    try:
        test_socket.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        test_socket.close()


def is_port_conflict_error(message):
    text = str(message or "").lower()
    indicators = (
        "address already in use",
        "bind for 0.0.0.0:",
        "driver failed programming external connectivity",
        "failed programming external connectivity",
        "listen tcp",
        "port is already allocated",
    )
    return any(indicator in text for indicator in indicators)


def pick_available_host_port(used_ports, protocol='tcp'):
    port_min, port_max = get_port_bounds()
    used = {int(port) for port in used_ports}
    candidates = [port for port in range(port_min, port_max + 1) if port not in used]
    random.shuffle(candidates)
    for selected in candidates:
        if not is_host_port_available(selected, protocol=protocol):
            used_ports.append(selected)
            continue
        used_ports.append(selected)
        return selected
    if not candidates:
        raise RuntimeError("No free Docker challenge ports are available in the configured range")
    raise RuntimeError("No bindable Docker challenge ports are available in the configured range")


def sanitize_container_name(value, max_length=64):
    sanitized = re.sub(r'[^a-zA-Z0-9_.-]+', '_', value).strip("_.-")
    return sanitized[:max_length] or "docker_challenge"


def build_container_name(image, owner_identity, challenge_id):
    """Build a bounded name whose uniqueness never depends on a truncated suffix."""
    image_hash = hashlib.sha256(str(image).encode("utf-8")).hexdigest()[:12]
    owner_hash = hashlib.sha256(str(owner_identity).encode("utf-8")).hexdigest()[:12]
    return sanitize_container_name(f"ctfd_c{challenge_id}_{owner_hash}_{image_hash}")


def build_stack_name(owner_identity, challenge_id):
    owner_hash = hashlib.sha256(str(owner_identity).encode("utf-8")).hexdigest()[:12]
    return sanitize_container_name(f"ctfd_s{challenge_id}_{owner_hash}")


def build_service_container_name(stack_id, service_name):
    service_slug = sanitize_container_name(str(service_name), max_length=24)
    service_hash = hashlib.sha256(str(service_name).encode("utf-8")).hexdigest()[:8]
    return sanitize_container_name(f"{stack_id}_{service_slug}_{service_hash}")


def normalize_string_list(value):
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def parse_optional_bool(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'")


def build_resource_host_config(service=None):
    host_config = {}

    memory_limit = None
    cpu_limit = None
    if service:
        memory_limit = service.get('memory_limit')
        cpu_limit = service.get('cpu_limit')

    if memory_limit is None:
        memory_limit = get_default_memory_limit()
    if cpu_limit is None:
        cpu_limit = get_default_cpu_limit()

    if memory_limit:
        host_config['Memory'] = int(memory_limit)
    if cpu_limit:
        host_config['NanoCpus'] = int(float(cpu_limit) * 1_000_000_000)
    if service:
        if service.get('cap_drop'):
            host_config['CapDrop'] = service['cap_drop']
        if service.get('read_only'):
            host_config['ReadonlyRootfs'] = True
        if service.get('pids_limit') is not None:
            host_config['PidsLimit'] = int(service['pids_limit'])

    return host_config


def build_env_list(env_map):
    if not env_map:
        return None
    return [f"{key}={value}" for key, value in sorted(env_map.items())]


def inspect_named_container(docker, container_name):
    return do_request(docker, f"/containers/{quote(container_name, safe='')}/json")


def inspect_named_network(docker, network_name):
    return do_request(docker, f"/networks/{quote(network_name, safe='')}")


def cleanup_conflicting_container(docker, container_name, expected_labels):
    inspect_response = inspect_named_container(docker, container_name)
    if inspect_response is None or inspect_response.status_code >= 400:
        return False

    details = parse_response_json(inspect_response, default={}) or {}
    labels = details.get("Config", {}).get("Labels", {}) or {}
    if labels.get("ctfd.plugin") != PLUGIN_LABEL:
        return False
    for key in ("ctfd.challenge.id", "ctfd.owner.id", "ctfd.owner.mode"):
        if expected_labels.get(key) and labels.get(key) != expected_labels.get(key):
            return False
    return delete_container(docker, details.get("Id") or container_name)


def cleanup_conflicting_network(docker, network_name, expected_labels):
    inspect_response = inspect_named_network(docker, network_name)
    if inspect_response is None or inspect_response.status_code >= 400:
        return False

    details = parse_response_json(inspect_response, default={}) or {}
    labels = details.get("Labels", {}) or {}
    if labels.get("ctfd.plugin") != PLUGIN_LABEL:
        return False
    for key in ("ctfd.challenge.id", "ctfd.owner.id", "ctfd.owner.mode", "ctfd.stack.id"):
        if expected_labels.get(key) and labels.get(key) != expected_labels.get(key):
            return False

    for container_id in (details.get("Containers") or {}).keys():
        if not container_id:
            continue
        container_response = do_request(docker, f"/containers/{quote(container_id, safe='')}/json")
        if container_response is None or container_response.status_code >= 400:
            return False
        container_details = parse_response_json(container_response, default={}) or {}
        container_labels = container_details.get("Config", {}).get("Labels", {}) or {}
        if container_labels.get("ctfd.plugin") != PLUGIN_LABEL:
            return False
        if container_labels.get("ctfd.stack.id") != expected_labels.get("ctfd.stack.id"):
            return False

    for container_id in (details.get("Containers") or {}).keys():
        delete_container(docker, container_id)
    network_id = details.get("Id") or network_name
    return delete_network(docker, network_id)


def create_container(docker, image, owner_identity, challenge_id, portbl, labels=None, env_vars=None, selected_ports=None):
    if not image:
        raise ValueError("No Docker image configured for this challenge")

    allowed_repositories = normalize_repo_selection(docker.repositories if docker else None)
    if not image_is_allowed(image, allowed_repositories):
        raise ValueError(f"Image '{image}' is not in the configured repository allow-list")

    available_ports = get_required_ports(docker, image)
    needed_ports = normalize_published_ports(selected_ports) if selected_ports is not None else available_ports
    if not needed_ports:
        raise ValueError(f"No published ports are configured for image '{image}'")
    unavailable = sorted(set(needed_ports) - set(available_ports))
    if unavailable:
        raise ValueError(
            f"Image '{image}' does not expose the selected port(s): {', '.join(unavailable)}"
        )

    container_name = build_container_name(image, owner_identity, challenge_id)
    retry_limit = get_port_retry_limit()
    last_error = None
    for attempt in range(retry_limit):
        exposed_ports = {}
        port_bindings = {}
        assigned_host_ports = []
        for port_spec in needed_ports:
            protocol = port_spec.split("/", 1)[1] if "/" in port_spec else "tcp"
            host_port = pick_available_host_port(portbl, protocol=protocol)
            exposed_ports[port_spec] = {}
            port_bindings[port_spec] = [{"HostPort": str(host_port)}]
            assigned_host_ports.append(f"{host_port}/{protocol}")

        host_config = {
            'PortBindings': port_bindings,
            **build_resource_host_config(),
        }
        payload = {
            "Image": image,
            "ExposedPorts": exposed_ports,
            "HostConfig": host_config,
            "Labels": labels or {},
        }
        env_list = build_env_list(env_vars)
        if env_list:
            payload["Env"] = env_list

        response = do_request_with_body(
            docker,
            f"/containers/create?name={quote(container_name, safe='')}",
            data=payload,
            method='POST',
        )
        if response is not None and response.status_code == 409 and cleanup_conflicting_container(docker, container_name, labels or {}):
            response = do_request_with_body(
                docker,
                f"/containers/create?name={quote(container_name, safe='')}",
                data=payload,
                method='POST',
            )
        if response is None or response.status_code >= 400:
            error_text = response.text if response is not None else "no response"
            last_error = RuntimeError(f"Failed to create container from '{image}': {error_text}")
            if not is_port_conflict_error(error_text) or attempt == retry_limit - 1:
                raise last_error
            continue

        result = parse_response_json(response, default={}) or {}
        container_id = result.get('Id')
        if not container_id:
            raise RuntimeError(f"Docker did not return a container id for '{image}'")

        start_response = do_request_with_body(docker, f"/containers/{container_id}/start", method='POST')
        if start_response is None or (start_response.status_code >= 400 and start_response.status_code != 304):
            delete_container(docker, container_id)
            error_text = start_response.text if start_response is not None else "no response"
            last_error = RuntimeError(f"Failed to start container '{container_id}': {error_text}")
            if not is_port_conflict_error(error_text) or attempt == retry_limit - 1:
                raise last_error
            continue

        return {
            'Id': container_id,
            'ports': assigned_host_ports,
            'config': payload,
        }

    raise last_error or RuntimeError(f"Failed to create container from '{image}'")


def delete_container(docker, instance_id):
    headers = {'Content-Type': "application/json"}
    response = do_request(docker, f'/containers/{instance_id}?force=true', headers=headers, method='DELETE')
    if response is None:
        return False
    return response.status_code == 404 or response.status_code < 400


def delete_network(docker, network_id):
    response = do_request(docker, f'/networks/{network_id}', method='DELETE')
    if response is None:
        return False
    return response.status_code == 404 or response.status_code < 400


def get_resource_instance_key(labels):
    labels = labels or {}
    direct = labels.get('ctfd.instance.key')
    if direct:
        return direct
    return get_instance_key(
        owner_mode=labels.get('ctfd.owner.mode'),
        owner_id=labels.get('ctfd.owner.id'),
        challenge_id=labels.get('ctfd.challenge.id'),
    )


def reconcile_docker_resources(docker):
    """Reconcile tracker/state rows with plugin-labeled Docker resources."""
    now = unix_time(datetime.utcnow())
    stale_deletions = DockerChallengeInstance.query.filter(
        DockerChallengeInstance.state.in_((INSTANCE_DELETING, INSTANCE_FAILED)),
        DockerChallengeInstance.updated_at <= now - INSTANCE_RECONCILE_GRACE,
    ).all()
    for record in stale_deletions:
        prior_state = record.state
        stale_trackers = DockerChallengeTracker.query.filter_by(instance_key=record.instance_key).all()
        if not stale_trackers:
            owner_field = (
                DockerChallengeTracker.team_id
                if record.owner_mode == 'teams'
                else DockerChallengeTracker.user_id
            )
            stale_trackers = DockerChallengeTracker.query.filter(
                owner_field == record.owner_id,
                DockerChallengeTracker.challenge_id == record.challenge_id,
            ).all()
        if stale_trackers:
            reason = "reconcile_delete" if prior_state == INSTANCE_DELETING else "reconcile_failed"
            delete_trackers(docker, stale_trackers, reason=reason, actor_role="system")
        elif prior_state == INSTANCE_DELETING:
            compare_and_set_instance_state(record, INSTANCE_STOPPED)

    # List resources after retrying stale deletions so this cycle never acts on
    # IDs that were just removed and possibly replaced by a new operation.
    container_response = do_request(docker, '/containers/json?all=1')
    network_response = do_request(docker, '/networks')
    if (
        container_response is None
        or network_response is None
        or container_response.status_code >= 400
        or network_response.status_code >= 400
    ):
        return False

    all_containers = parse_response_json(container_response, default=[])
    all_networks = parse_response_json(network_response, default=[])
    if not isinstance(all_containers, list) or not isinstance(all_networks, list):
        return False

    plugin_containers = {
        item.get('Id'): item
        for item in all_containers
        if item.get('Id') and (item.get('Labels') or {}).get('ctfd.plugin') == PLUGIN_LABEL
    }
    all_container_ids = {
        item.get('Id')
        for item in all_containers
        if item.get('Id')
    }
    plugin_networks = {
        item.get('Id'): item
        for item in all_networks
        if item.get('Id') and (item.get('Labels') or {}).get('ctfd.plugin') == PLUGIN_LABEL
    }
    trackers = DockerChallengeTracker.query.all()
    trackers_by_id = {tracker.instance_id: tracker for tracker in trackers if tracker.instance_id}
    records = {record.instance_key: record for record in DockerChallengeInstance.query.all()}
    audit_messages = []
    failed_keys = set()

    # Tracker rows whose Docker container vanished are stale. Remove them and
    # mark the logical instance failed so the next start can recover cleanly.
    for instance_id, tracker in list(trackers_by_id.items()):
        if instance_id in all_container_ids:
            continue
        inspect_response = do_request(
            docker,
            f"/containers/{quote(instance_id, safe='')}/json",
        )
        if inspect_response is None or inspect_response.status_code != 404:
            # A container may have been created after the list call. Keep the
            # tracker on transient Docker/API errors and confirm only 404s.
            continue
        failed_keys.add(tracker.instance_key or get_instance_key(
            owner_mode='teams' if tracker.team_id else 'users',
            owner_id=tracker.team_id or tracker.user_id,
            challenge_id=tracker.challenge_id,
        ))
        db.session.delete(tracker)
        audit_messages.append(("reconcile_missing", f"Removed stale tracker for missing container {instance_id}"))

    # Delete stopped/dead tracked containers and any untracked plugin-owned
    # containers, except for a recent matching creation operation.
    for instance_id, item in plugin_containers.items():
        labels = item.get('Labels') or {}
        instance_key = get_resource_instance_key(labels)
        tracker = trackers_by_id.get(instance_id)
        record = records.get(instance_key)
        protected_creation = bool(
            record
            and record.state == INSTANCE_CREATING
            and now - int(record.updated_at or 0) < INSTANCE_RECONCILE_GRACE
            and labels.get('ctfd.operation.token') == record.operation_token
        )
        docker_state = str(item.get('State') or '').lower()
        if tracker is not None and docker_state in {'running', 'paused', 'restarting'}:
            continue
        if tracker is None and protected_creation:
            continue
        if delete_container(docker, instance_id):
            if tracker is not None:
                db.session.delete(tracker)
            if tracker is not None and instance_key:
                failed_keys.add(instance_key)
            action = "reconcile_exited" if tracker is not None else "reconcile_orphan"
            audit_messages.append((action, f"Removed Docker container {instance_id}"))

    db.session.flush()
    remaining_network_ids = {
        tracker.network_id
        for tracker in DockerChallengeTracker.query.all()
        if tracker.network_id
    }
    for network_id, item in plugin_networks.items():
        if network_id in remaining_network_ids:
            continue
        labels = item.get('Labels') or {}
        instance_key = get_resource_instance_key(labels)
        record = records.get(instance_key)
        protected_creation = bool(
            record
            and record.state == INSTANCE_CREATING
            and now - int(record.updated_at or 0) < INSTANCE_RECONCILE_GRACE
            and labels.get('ctfd.operation.token') == record.operation_token
        )
        if protected_creation:
            continue
        if delete_network(docker, network_id):
            audit_messages.append(("reconcile_orphan", f"Removed Docker network {network_id}"))

    for instance_key, record in records.items():
        tracker_count = DockerChallengeTracker.query.filter_by(instance_key=instance_key).count()
        age = now - int(record.updated_at or 0)
        if instance_key in failed_keys or (record.state == INSTANCE_RUNNING and tracker_count == 0):
            compare_and_set_instance_state(
                record,
                INSTANCE_FAILED,
                error="Docker runtime state required reconciliation",
                commit=False,
            )
        elif record.state == INSTANCE_CREATING and age >= INSTANCE_RECONCILE_GRACE:
            compare_and_set_instance_state(
                record,
                INSTANCE_FAILED,
                error="Docker creation did not complete",
                commit=False,
            )
        elif record.state == INSTANCE_DELETING and age >= INSTANCE_RECONCILE_GRACE and tracker_count == 0:
            compare_and_set_instance_state(record, INSTANCE_STOPPED, commit=False)

    db.session.commit()
    for action, message in audit_messages:
        log_audit_event(action, actor_role="system", message=message)
    return True


def parse_compose_content(yaml_str):
    """Parse a docker-compose.yml string into a normalized structure.
    Returns dict with 'services' key. Raises ValueError on invalid/unsupported content."""
    if not isinstance(yaml_str, str):
        raise ValueError("Compose content must be text")
    if len(yaml_str.encode("utf-8", errors="replace")) > MAX_COMPOSE_BYTES:
        raise ValueError(f"Compose content may not exceed {MAX_COMPOSE_BYTES // 1024} KB")

    try:
        alias_count = sum(
            1 for token in yaml.scan(yaml_str)
            if isinstance(token, AliasToken)
        )
        if alias_count > MAX_YAML_ALIASES:
            raise ValueError(
                f"Compose content may contain at most {MAX_YAML_ALIASES} YAML aliases"
            )
        doc = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}")

    if not isinstance(doc, dict):
        raise ValueError("Compose file must be a YAML mapping")

    unsupported_top_level = set(doc.keys()) - {'services', 'version', 'name'}
    if unsupported_top_level:
        raise ValueError(
            f"Unsupported top-level Compose keys: {', '.join(sorted(map(str, unsupported_top_level)))}"
        )

    services = doc.get('services', {})
    if not isinstance(services, dict):
        raise ValueError("Compose 'services' must be a YAML mapping")
    if not services:
        raise ValueError("Compose file must define at least one service under 'services:'")
    if len(services) > get_max_services_per_stack():
        raise ValueError(
            f"Compose stacks may define at most {get_max_services_per_stack()} services"
        )

    result = {}
    total_published_ports = 0
    for name, svc in services.items():
        if not isinstance(name, str) or not SERVICE_NAME_RE.fullmatch(name):
            raise ValueError(
                f"Service name '{name}' must start with a letter or digit and contain only letters, digits, '.', '_', or '-'"
            )
        if not isinstance(svc, dict):
            raise ValueError(f"Service '{name}' must be a mapping")

        disallowed = COMPOSE_DISALLOWED_KEYS & set(svc.keys())
        if disallowed:
            raise ValueError(f"Service '{name}' uses disallowed keys: {', '.join(sorted(disallowed))}")
        unsupported = set(svc.keys()) - COMPOSE_ALLOWED_KEYS
        if unsupported:
            raise ValueError(f"Service '{name}' uses unsupported keys: {', '.join(sorted(map(str, unsupported)))}")

        image = svc.get('image')
        if not isinstance(image, str) or not image.strip():
            raise ValueError(f"Service '{name}' must specify an 'image' (build is not supported)")
        image = image.strip()
        if len(image) > 255:
            raise ValueError(f"Service '{name}' image reference is too long")

        # Parse environment
        env = {}
        raw_env = svc.get('environment', {})
        if isinstance(raw_env, list):
            for item in raw_env:
                if not isinstance(item, str) or '=' not in item:
                    raise ValueError(
                        f"Service '{name}' environment list entries must use KEY=value syntax"
                    )
                k, _, v = str(item).partition('=')
                if not ENV_NAME_RE.fullmatch(k):
                    raise ValueError(f"Service '{name}' has invalid environment variable name '{k}'")
                env[k] = v
        elif isinstance(raw_env, dict):
            for key, value in raw_env.items():
                key = str(key)
                if not ENV_NAME_RE.fullmatch(key):
                    raise ValueError(f"Service '{name}' has invalid environment variable name '{key}'")
                env[key] = "" if value is None else str(value)
        else:
            raise ValueError(f"Service '{name}' environment must be a mapping or list")

        # Parse ports - only services with ports are publicly exposed
        ports = []
        raw_ports = svc.get('ports', [])
        if raw_ports is None:
            raw_ports = []
        if not isinstance(raw_ports, list):
            raise ValueError(f"Service '{name}' ports must be a list")
        seen_ports = set()
        for p in raw_ports:
            if isinstance(p, dict):
                # Long syntax: {target: 80, published: 8080, protocol: tcp}
                unsupported_port_keys = set(p.keys()) - {'target', 'published', 'protocol'}
                if unsupported_port_keys:
                    raise ValueError(
                        f"Service '{name}' long-form port entry uses unsupported keys: "
                        f"{', '.join(sorted(map(str, unsupported_port_keys)))}"
                    )
                if 'target' not in p:
                    raise ValueError(f"Service '{name}' long-form port entries require 'target'")
                if isinstance(p['target'], bool):
                    raise ValueError(f"Service '{name}' has invalid target port '{p.get('target')}'")
                try:
                    target = int(p['target'])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Service '{name}' has invalid target port '{p.get('target')}'") from exc
                published = p.get('published')
                if published not in (None, ''):
                    if isinstance(published, bool):
                        raise ValueError(f"Service '{name}' has invalid published port '{published}'")
                    try:
                        published = int(published)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"Service '{name}' has invalid published port '{published}'") from exc
                    if not (1 <= published <= 65535):
                        raise ValueError(f"Service '{name}' published port must be between 1 and 65535")
                protocol = str(p.get('protocol', 'tcp')).lower()
            else:
                # Short syntax: "8080:80" or "80"
                if not isinstance(p, (str, int)) or isinstance(p, bool):
                    raise ValueError(f"Service '{name}' has an invalid port entry '{p}'")
                raw_port = str(p).strip()
                port_match = re.fullmatch(
                    r"(?:(\d{1,5}):)?(\d{1,5})(?:/(tcp|udp))?",
                    raw_port,
                    flags=re.IGNORECASE,
                )
                if not port_match:
                    raise ValueError(f"Service '{name}' has invalid port syntax '{raw_port}'")
                published = port_match.group(1)
                if published is not None and not (1 <= int(published) <= 65535):
                    raise ValueError(f"Service '{name}' published port must be between 1 and 65535")
                target = int(port_match.group(2))
                protocol = (port_match.group(3) or 'tcp').lower()
            if not (1 <= target <= 65535):
                raise ValueError(f"Service '{name}' target port must be between 1 and 65535")
            if protocol not in {'tcp', 'udp'}:
                raise ValueError(f"Service '{name}' port protocol must be tcp or udp")
            port_key = (target, protocol)
            if port_key in seen_ports:
                raise ValueError(f"Service '{name}' publishes duplicate port {target}/{protocol}")
            seen_ports.add(port_key)
            ports.append({'target': target, 'protocol': protocol})
        total_published_ports += len(ports)

        depends_on = svc.get('depends_on', [])
        if isinstance(depends_on, dict):
            unsupported_dependency_options = [
                dep for dep, options in depends_on.items()
                if options not in (None, {})
            ]
            if unsupported_dependency_options:
                raise ValueError(
                    f"Service '{name}' uses unsupported depends_on options for: "
                    f"{', '.join(map(str, unsupported_dependency_options))}"
                )
            depends_on = list(depends_on.keys())
        elif depends_on is None:
            depends_on = []
        elif not isinstance(depends_on, list):
            raise ValueError(f"Service '{name}' depends_on must be a list or mapping")
        depends_on = [str(dep) for dep in depends_on]

        command = svc.get('command')
        entrypoint = svc.get('entrypoint')
        for field_name, field_value in (("command", command), ("entrypoint", entrypoint)):
            if field_value is not None and not isinstance(field_value, (str, list)):
                raise ValueError(f"Service '{name}' {field_name} must be a string or list")
            if isinstance(field_value, list) and not all(
                isinstance(part, (str, int, float)) and not isinstance(part, bool)
                for part in field_value
            ):
                raise ValueError(f"Service '{name}' {field_name} list contains an invalid value")
        if isinstance(command, list):
            command = [str(part) for part in command]
        if isinstance(entrypoint, list):
            entrypoint = [str(part) for part in entrypoint]

        memory_limit = parse_memory_value(svc.get('mem_limit'))
        if memory_limit is not None and memory_limit > MAX_SERVICE_MEMORY_BYTES:
            raise ValueError(f"Service '{name}' memory limit exceeds 16 GiB")
        cpu_limit = parse_cpu_value(svc.get('cpus'))
        if cpu_limit is not None and cpu_limit > MAX_SERVICE_CPUS:
            raise ValueError(f"Service '{name}' CPU limit exceeds {MAX_SERVICE_CPUS:g} CPUs")
        pids_limit = parse_optional_int(svc.get('pids_limit'))
        if pids_limit is not None and not (1 <= pids_limit <= MAX_SERVICE_PIDS):
            raise ValueError(f"Service '{name}' pids_limit must be between 1 and {MAX_SERVICE_PIDS}")
        cap_drop = normalize_string_list(svc.get('cap_drop'))
        if len(cap_drop) > 64 or any(not re.fullmatch(r"[A-Za-z0-9_]+", cap) for cap in cap_drop):
            raise ValueError(f"Service '{name}' cap_drop contains an invalid capability name")

        result[name] = {
            'image': image,
            'environment': env,
            'ports': ports,
            'depends_on': depends_on,
            'command': command,
            'entrypoint': entrypoint,
            'memory_limit': memory_limit,
            'cpu_limit': cpu_limit,
            'cap_drop': cap_drop,
            'read_only': bool(parse_optional_bool(svc.get('read_only'))),
            'pids_limit': pids_limit,
        }

    if total_published_ports > get_max_published_ports():
        raise ValueError(
            f"Compose stacks may publish at most {get_max_published_ports()} ports in total"
        )
    for name, svc in result.items():
        for dep in svc.get('depends_on', []):
            if dep not in result:
                raise ValueError(f"Service '{name}' depends on undefined service '{dep}'")

    _topo_sort_services(result)

    return {'services': result}


def _topo_sort_services(services):
    """Topological sort of services by depends_on. Returns list of service names."""
    visited = set()
    visiting = set()
    order = []

    def visit(name):
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"Compose services contain a dependency cycle involving '{name}'")
        visiting.add(name)
        svc = services.get(name, {})
        for dep in svc.get('depends_on', []):
            if dep in services:
                visit(dep)
        visiting.remove(name)
        visited.add(name)
        order.append(name)

    for name in services:
        visit(name)
    return order


def create_stack(docker, compose_yaml_str, owner_identity, challenge_id, portbl, labels=None, runtime_env=None):
    """Create a multi-container stack from a compose YAML string.
    Returns dict with stack_id, network_id, and containers list."""
    spec = parse_compose_content(compose_yaml_str)
    services = spec['services']
    allowed_repositories = normalize_repo_selection(docker.repositories if docker else None)
    for service_name, service in services.items():
        if not image_is_allowed(service['image'], allowed_repositories):
            raise ValueError(
                f"Service '{service_name}' uses image '{service['image']}' which is not in the configured repository allow-list"
            )

    stack_id = build_stack_name(owner_identity, challenge_id)
    host_value = get_connection_host_value(docker)
    network_labels = {
        'ctfd.plugin': PLUGIN_LABEL,
        'ctfd.stack.id': stack_id,
        **(labels or {}),
    }
    order = _topo_sort_services(services)
    last_error = None
    retry_limit = get_port_retry_limit()

    for attempt in range(retry_limit):
        network_id = ""
        created = []
        try:
            net_resp = do_request_with_body(docker, '/networks/create', data={
                'Name': stack_id,
                'Driver': 'bridge',
                'Labels': network_labels,
            })
            if net_resp is not None and net_resp.status_code == 409 and cleanup_conflicting_network(docker, stack_id, network_labels):
                net_resp = do_request_with_body(docker, '/networks/create', data={
                    'Name': stack_id,
                    'Driver': 'bridge',
                    'Labels': network_labels,
                })
            if net_resp is None or net_resp.status_code >= 400:
                raise RuntimeError(f"Failed to create network: {net_resp.text if net_resp else 'no response'}")
            network_id = net_resp.json().get('Id', '')

            port_map = {}
            placeholder_port_map = {}
            for svc_name in order:
                svc = services[svc_name]
                port_map[svc_name] = {}
                placeholder_port_map[svc_name] = {}
                for p in svc['ports']:
                    host_port = pick_available_host_port(portbl, protocol=p['protocol'])
                    port_key = (p['target'], p['protocol'])
                    port_map[svc_name][port_key] = host_port
                    placeholder_port_map[svc_name].setdefault(p['target'], host_port)

            def _replace_port_placeholders(value):
                value = str(value)

                def repl_full(match):
                    service_name, target_port = match.group(1), int(match.group(2))
                    return str(placeholder_port_map.get(service_name, {}).get(target_port, match.group(0)))

                def repl_svc(match):
                    service_name = match.group(1)
                    ports = placeholder_port_map.get(service_name, {})
                    return str(next(iter(ports.values()))) if ports else match.group(0)

                value = re.sub(r'\{\{PORT_([A-Za-z0-9_.-]+)_(\d+)\}\}', repl_full, value)
                value = re.sub(r'\{\{PORT_([A-Za-z0-9_.-]+)\}\}', repl_svc, value)
                return value.replace('{{HOST}}', host_value)

            for svc_name in order:
                svc = services[svc_name]
                container_name = build_service_container_name(stack_id, svc_name)
                own_ports = placeholder_port_map.get(svc_name, {})
                own_first_port = str(next(iter(own_ports.values()))) if own_ports else ''

                merged_env = {}
                for key, value in svc['environment'].items():
                    value = _replace_port_placeholders(value).replace('{{PORT}}', own_first_port)
                    merged_env[key] = value
                # Runtime identity and HMAC values are authoritative and must
                # not be shadowed by duplicate Compose environment entries.
                merged_env.update(runtime_env or {})
                env_list = build_env_list(merged_env)

                cmd = svc.get('command')
                if isinstance(cmd, str):
                    cmd = _replace_port_placeholders(cmd).replace('{{PORT}}', own_first_port)
                entrypoint = svc.get('entrypoint')
                if isinstance(entrypoint, str):
                    entrypoint = _replace_port_placeholders(entrypoint).replace('{{PORT}}', own_first_port)

                exposed_ports = {}
                port_bindings = {}
                assigned_host_ports = []
                for p in svc['ports']:
                    key = f"{p['target']}/{p['protocol']}"
                    exposed_ports[key] = {}
                    host_port = port_map[svc_name][(p['target'], p['protocol'])]
                    port_bindings[key] = [{'HostPort': str(host_port)}]
                    assigned_host_ports.append(f"{host_port}/{p['protocol']}")

                config = {
                    'Image': svc['image'],
                    'Env': env_list,
                    'HostConfig': {
                        'NetworkMode': stack_id,
                        'PortBindings': port_bindings if port_bindings else None,
                        **build_resource_host_config(svc),
                    },
                    'NetworkingConfig': {
                        'EndpointsConfig': {
                            stack_id: {
                                'Aliases': [svc_name],
                            }
                        }
                    },
                    'Labels': {
                        'ctfd.plugin': PLUGIN_LABEL,
                        'ctfd.challenge.id': str(challenge_id),
                        'ctfd.stack.id': stack_id,
                        'ctfd.service.name': svc_name,
                        **(labels or {}),
                    },
                }

                if exposed_ports:
                    config['ExposedPorts'] = exposed_ports
                if cmd:
                    config['Cmd'] = shlex.split(cmd) if isinstance(cmd, str) else cmd
                if entrypoint:
                    config['Entrypoint'] = shlex.split(entrypoint) if isinstance(entrypoint, str) else entrypoint

                config['HostConfig'] = {k: v for k, v in config['HostConfig'].items() if v is not None}

                resp = do_request_with_body(
                    docker,
                    f"/containers/create?name={quote(container_name, safe='')}",
                    data=config,
                )
                if resp is not None and resp.status_code == 409 and cleanup_conflicting_container(docker, container_name, config['Labels']):
                    resp = do_request_with_body(
                        docker,
                        f"/containers/create?name={quote(container_name, safe='')}",
                        data=config,
                    )
                if resp is None or resp.status_code >= 400:
                    error_text = resp.text if resp else 'no response'
                    raise RuntimeError(f"Failed to create container '{svc_name}': {error_text}")

                container_id = resp.json()['Id']
                start_resp = do_request_with_body(docker, f'/containers/{container_id}/start')
                if start_resp is None or (start_resp.status_code >= 400 and start_resp.status_code != 304):
                    error_text = start_resp.text if start_resp is not None else 'no response'
                    delete_container(docker, container_id)
                    raise RuntimeError(f"Failed to start container '{svc_name}': {error_text}")

                created.append({
                    'service_name': svc_name,
                    'instance_id': container_id,
                    'image': svc['image'],
                    'ports': ','.join(assigned_host_ports) if assigned_host_ports else '',
                })

            return {
                'stack_id': stack_id,
                'network_id': network_id,
                'containers': created,
            }
        except Exception as exc:
            last_error = exc
            for container in created:
                try:
                    delete_container(docker, container['instance_id'])
                except Exception:
                    pass
            if network_id:
                try:
                    delete_network(docker, network_id)
                except Exception:
                    pass
            if not is_port_conflict_error(exc) or attempt == retry_limit - 1:
                raise

    raise last_error or RuntimeError("Failed to create compose stack")


def delete_stack(docker, stack_id, entries=None):
    """Delete all containers and network for a compose stack, and clean up DB."""
    entries = list(entries) if entries is not None else DockerChallengeTracker.query.filter_by(stack_id=stack_id).all()
    network_id = None
    success = True
    for entry in entries:
        if entry.network_id:
            network_id = entry.network_id
        try:
            if not delete_container(docker, entry.instance_id):
                success = False
        except:
            traceback.print_exc()
            success = False
    if network_id:
        try:
            if not delete_network(docker, network_id):
                success = False
        except:
            traceback.print_exc()
            success = False
    if success:
        entry_ids = [entry.id for entry in entries if entry.id is not None]
        if entry_ids:
            persisted_entries = DockerChallengeTracker.query.filter(
                DockerChallengeTracker.id.in_(entry_ids)
            ).all()
            for persisted_entry in persisted_entries:
                db.session.delete(persisted_entry)
        db.session.commit()
    return success


def delete_trackers(docker, trackers, reason="stop", participant=None, actor_role="participant", actor_id=None, actor_name=None):
    if not docker:
        return trackers

    trackers = list(trackers)
    expanded_trackers = {tracker.id: tracker for tracker in trackers if tracker.id is not None}
    transient_trackers = [tracker for tracker in trackers if tracker.id is None]
    for tracker in trackers:
        if not tracker.stack_id:
            continue
        stack_query = DockerChallengeTracker.query.filter_by(stack_id=tracker.stack_id)
        if tracker.instance_key:
            stack_query = stack_query.filter_by(instance_key=tracker.instance_key)
        elif tracker.team_id:
            stack_query = stack_query.filter_by(team_id=tracker.team_id)
        elif tracker.user_id:
            stack_query = stack_query.filter_by(user_id=tracker.user_id)
        if tracker.challenge_id is not None:
            stack_query = stack_query.filter_by(challenge_id=tracker.challenge_id)
        elif tracker.challenge:
            stack_query = stack_query.filter_by(challenge=tracker.challenge)
        for stack_entry in stack_query.all():
            expanded_trackers[stack_entry.id] = stack_entry
    tracker_rows = list(expanded_trackers.values()) + transient_trackers
    tracker_fields = (
        'id', 'team_id', 'user_id', 'docker_image', 'timestamp', 'revert_time',
        'instance_id', 'ports', 'host', 'challenge', 'challenge_id',
        'service_name', 'stack_id', 'network_id', 'ports_json', 'instance_key',
    )
    trackers = [
        SimpleNamespace(**{
            field: getattr(tracker, field, None)
            for field in tracker_fields
        })
        for tracker in tracker_rows
    ]

    def tracker_instance_ref(tracker):
        owner_mode = "teams" if tracker.team_id else "users" if tracker.user_id else None
        owner_id = tracker.team_id or tracker.user_id
        if owner_mode and owner_id and tracker.challenge_id is not None:
            return owner_mode, str(owner_id), int(tracker.challenge_id)
        return None

    def tracker_stack_scope(tracker):
        return tracker.instance_key or tracker_instance_ref(tracker) or (
            "legacy",
            str(tracker.team_id or tracker.user_id or ""),
            str(tracker.challenge or ""),
        )

    trackers_by_ref = {}
    for tracker in trackers:
        ref = tracker_instance_ref(tracker)
        if ref is not None:
            trackers_by_ref.setdefault(ref, []).append(tracker)

    operation_tokens = {}
    skipped_refs = set()
    for ref, referenced_trackers in trackers_by_ref.items():
        owner_mode, owner_id, challenge_id = ref
        acquire_owner_lock_by_identity(owner_mode, owner_id)
        record = get_instance_record_by_identity(
            owner_mode,
            owner_id,
            challenge_id,
            for_update=True,
        )
        if reason == "expire" and record is not None and record.state in {
            INSTANCE_CREATING,
            INSTANCE_DELETING,
        }:
            skipped_refs.add(ref)
            db.session.rollback()
            continue

        operation_token = (
            record.operation_token
            if record is not None and record.state == INSTANCE_DELETING and record.operation_token
            else secrets.token_hex(16)
        )
        now = unix_time(datetime.utcnow())
        if record is None:
            representative = referenced_trackers[0]
            timestamps = []
            for tracker in referenced_trackers:
                try:
                    timestamps.append(int(tracker.timestamp))
                except (TypeError, ValueError):
                    pass
            record = DockerChallengeInstance(
                owner_mode=owner_mode,
                owner_id=owner_id,
                challenge_id=challenge_id,
                challenge_name=representative.challenge or f"Challenge {challenge_id}",
                instance_key=get_instance_key(
                    owner_mode=owner_mode,
                    owner_id=owner_id,
                    challenge_id=challenge_id,
                ),
                state=INSTANCE_DELETING,
                operation_token=operation_token,
                created_at=min(timestamps) if timestamps else now,
                updated_at=now,
            )
            db.session.add(record)
        else:
            update_instance_state(
                record,
                INSTANCE_DELETING,
                operation_token=operation_token,
                commit=False,
            )
        db.session.flush()
        tracker_ids = [tracker.id for tracker in referenced_trackers if tracker.id is not None]
        if tracker_ids:
            DockerChallengeTracker.query.filter(
                DockerChallengeTracker.id.in_(tracker_ids),
                DockerChallengeTracker.instance_key.is_(None),
            ).update(
                {"instance_key": record.instance_key},
                synchronize_session=False,
            )
            for tracker in referenced_trackers:
                if not tracker.instance_key:
                    tracker.instance_key = record.instance_key
        db.session.commit()
        operation_tokens[ref] = operation_token

    if skipped_refs:
        trackers = [
            tracker for tracker in trackers
            if tracker_instance_ref(tracker) not in skipped_refs
        ]

    instance_refs = set(operation_tokens)
    if not trackers:
        return []

    failed = []
    deleted_stacks = set()
    deleted_instances = []
    for tracker in trackers:
        if tracker.stack_id:
            stack_key = (tracker_stack_scope(tracker), tracker.stack_id)
            if stack_key not in deleted_stacks:
                stack_entries = [
                    entry for entry in trackers
                    if entry.stack_id == tracker.stack_id
                    and tracker_stack_scope(entry) == tracker_stack_scope(tracker)
                ]
                if delete_stack(docker, tracker.stack_id, entries=stack_entries):
                    for entry in stack_entries:
                        log_audit_event(
                            reason,
                            tracker=entry,
                            participant=participant,
                            actor_role=actor_role,
                            actor_id=actor_id,
                            actor_name=actor_name,
                            message=f"Removed stack {tracker.stack_id}",
                            ip_address=get_current_request_ip(),
                        )
                else:
                    failed.extend(stack_entries)
                    for entry in stack_entries:
                        log_audit_event(
                            reason,
                            status="error",
                            tracker=entry,
                            participant=participant,
                            actor_role=actor_role,
                            actor_id=actor_id,
                            actor_name=actor_name,
                            message=f"Failed to remove stack {tracker.stack_id}",
                            ip_address=get_current_request_ip(),
                        )
                deleted_stacks.add(stack_key)
        else:
            if delete_container(docker, tracker.instance_id):
                deleted_instances.append(tracker.instance_id)
                log_audit_event(
                    reason,
                    tracker=tracker,
                    participant=participant,
                    actor_role=actor_role,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    message=f"Removed container {tracker.instance_id}",
                    ip_address=get_current_request_ip(),
                )
            else:
                failed.append(tracker)
                log_audit_event(
                    reason,
                    status="error",
                    tracker=tracker,
                    participant=participant,
                    actor_role=actor_role,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    message=f"Failed to remove container {tracker.instance_id}",
                    ip_address=get_current_request_ip(),
                )

    if deleted_instances:
        deleted_tracker_rows = DockerChallengeTracker.query.filter(
            DockerChallengeTracker.instance_id.in_(deleted_instances)
        ).all()
        for deleted_tracker in deleted_tracker_rows:
            db.session.delete(deleted_tracker)
        db.session.commit()

    failed_refs = set()
    for tracker in failed:
        owner_mode = "teams" if tracker.team_id else "users" if tracker.user_id else None
        owner_id = tracker.team_id or tracker.user_id
        if owner_mode and owner_id and tracker.challenge_id is not None:
            failed_refs.add((owner_mode, str(owner_id), int(tracker.challenge_id)))

    for owner_mode, owner_id, challenge_id in instance_refs:
        acquire_owner_lock_by_identity(owner_mode, owner_id)
        record = get_instance_record_by_identity(
            owner_mode,
            owner_id,
            challenge_id,
            for_update=True,
        )
        if record is None:
            db.session.rollback()
            continue
        if record.operation_token != operation_tokens.get((owner_mode, owner_id, challenge_id)):
            db.session.rollback()
            continue
        remaining = DockerChallengeTracker.query.filter(
            DockerChallengeTracker.challenge_id == challenge_id,
            (DockerChallengeTracker.team_id == owner_id) if owner_mode == "teams"
            else (DockerChallengeTracker.user_id == owner_id),
        ).count()
        if (owner_mode, owner_id, challenge_id) in failed_refs or remaining:
            update_instance_state(record, INSTANCE_FAILED, error=f"Lifecycle operation '{reason}' was incomplete", commit=False)
        else:
            update_instance_state(record, INSTANCE_STOPPED, commit=False)
        db.session.commit()
    return failed


def normalize_challenge_payload(data):
    ports_field_present = 'published_ports' in data or 'published_ports_present' in data
    if hasattr(data, 'getlist'):
        submitted_ports = data.getlist('published_ports')
    else:
        submitted_ports = data.get('published_ports') if ports_field_present else None
    normalized = dict(data)
    mode = normalized.get('docker_mode')
    compose_content = (normalized.get('compose_content') or '').strip()
    docker_image = (normalized.get('docker_image') or '').strip()
    flag_mode = (normalized.get('flag_mode') or 'static').strip().lower()
    flag_template = (normalized.get('flag_template') or '').strip()
    docker_config = get_docker_config()
    allowed_repositories = normalize_repo_selection(docker_config.repositories if docker_config else None)

    if flag_mode not in {'static', 'hmac'}:
        abort(400, "Unsupported flag mode")
    normalized['flag_mode'] = flag_mode
    normalized['flag_template'] = flag_template or DEFAULT_HMAC_TEMPLATE if flag_mode == 'hmac' else None
    if flag_mode == 'hmac':
        try:
            get_flag_template(type("ChallengeTemplate", (), {"flag_template": normalized['flag_template']})())
        except ValueError as exc:
            abort(400, str(exc))

    if mode == 'compose' or (mode is None and compose_content):
        if not compose_content:
            abort(400, "Compose mode requires docker-compose YAML content")

        try:
            spec = parse_compose_content(compose_content)
        except ValueError as exc:
            abort(400, f"Invalid compose configuration: {exc}")

        for service_name, service in spec['services'].items():
            if not image_is_allowed(service['image'], allowed_repositories):
                abort(
                    400,
                    f"Service '{service_name}' uses image '{service['image']}' which is not in the configured repository allow-list",
                )

        normalized['compose_content'] = compose_content
        normalized['docker_image'] = 'compose'
        normalized['published_ports'] = None
    else:
        if not docker_image:
            abort(400, "Single-image mode requires a Docker image")
        if not image_is_allowed(docker_image, allowed_repositories):
            abort(400, f"Image '{docker_image}' is not in the configured repository allow-list")
        normalized['docker_image'] = docker_image
        normalized['compose_content'] = None
        if ports_field_present:
            try:
                selected_ports = normalize_published_ports(submitted_ports)
            except ValueError as exc:
                abort(400, str(exc))
            if not selected_ports:
                abort(400, "Select at least one exposed container port")
            if len(selected_ports) > get_max_published_ports():
                abort(
                    400,
                    f"Select at most {get_max_published_ports()} published container ports",
                )
            if docker_config is None:
                abort(503, "Docker must be configured before selecting published ports")
            try:
                available_ports = get_required_ports(docker_config, docker_image)
            except RuntimeError as exc:
                abort(400, str(exc))
            unavailable = sorted(set(selected_ports) - set(available_ports))
            if unavailable:
                abort(
                    400,
                    f"Image '{docker_image}' does not expose the selected port(s): {', '.join(unavailable)}",
                )
            normalized['published_ports'] = json.dumps(selected_ports)

    normalized.pop('docker_mode', None)
    normalized.pop('published_ports_present', None)
    return normalized


class DockerChallengeType(BaseChallenge):
    id = "docker"
    name = "docker"
    templates = {
        'create': '/plugins/docker_challenges/assets/create.html',
        'update': '/plugins/docker_challenges/assets/update.html',
        'view': '/plugins/docker_challenges/assets/view.html',
    }
    scripts = {
        'create': '/plugins/docker_challenges/assets/create.js',
        'update': '/plugins/docker_challenges/assets/update.js',
        'view': '/plugins/docker_challenges/assets/view.js',
    }
    route = '/plugins/docker_challenges/assets'
    blueprint = Blueprint('docker_challenges', __name__, template_folder='templates', static_folder='assets')

    @staticmethod
    def update(challenge, request):
        """
		This method is used to update the information associated with a challenge. This should be kept strictly to the
		Challenges table and any child tables.

		:param challenge:
		:param request:
		:return:
		"""
        data = request.form or request.get_json()
        skip_fields = {'nonce', 'csrf_token'}
        data = normalize_challenge_payload(data)
        for attr, value in data.items():
            if attr not in skip_fields:
                setattr(challenge, attr, value)

        DockerChallengeInstance.query.filter_by(challenge_id=challenge.id).update(
            {"challenge_name": challenge.name},
            synchronize_session=False,
        )

        db.session.commit()
        return challenge

    @staticmethod
    def delete(challenge):
        """
		This method is used to delete the resources used by a challenge.
		NOTE: Will need to kill all containers here

		:param challenge:
		:return:
		"""
        docker = get_docker_config()
        trackers = DockerChallengeTracker.query.filter(
            (DockerChallengeTracker.challenge_id == challenge.id) |
            (DockerChallengeTracker.challenge == challenge.name)
        ).all()
        if trackers:
            actor = get_current_user() if has_request_context() else None
            failed = delete_trackers(
                docker,
                trackers,
                reason="challenge_delete",
                actor_role="admin" if actor else "system",
                actor_id=actor.id if actor else None,
                actor_name=getattr(actor, "name", None),
            )
            if failed:
                raise RuntimeError("Failed to remove all active Docker instances before deleting the challenge")

        Fails.query.filter_by(challenge_id=challenge.id).delete()
        Solves.query.filter_by(challenge_id=challenge.id).delete()
        Flags.query.filter_by(challenge_id=challenge.id).delete()
        files = ChallengeFiles.query.filter_by(challenge_id=challenge.id).all()
        for f in files:
            delete_file(f.id)
        ChallengeFiles.query.filter_by(challenge_id=challenge.id).delete()
        Tags.query.filter_by(challenge_id=challenge.id).delete()
        Hints.query.filter_by(challenge_id=challenge.id).delete()
        DockerChallengeInstance.query.filter_by(challenge_id=challenge.id).delete()
        DockerChallenge.query.filter_by(id=challenge.id).delete()
        Challenges.query.filter_by(id=challenge.id).delete()
        db.session.commit()

    @staticmethod
    def read(challenge):
        """
		This method is in used to access the data of a challenge in a format processable by the front end.

		:param challenge:
		:return: Challenge object, data dictionary to be returned to the user
		"""
        challenge = DockerChallenge.query.filter_by(id=challenge.id).first()
        data = {
            'id': challenge.id,
            'name': challenge.name,
            'value': challenge.value,
            'docker_image': challenge.docker_image,
            'compose_content': challenge.compose_content,
            'published_ports': decode_published_ports(challenge),
            'flag_mode': challenge.flag_mode or 'static',
            'flag_template': challenge.flag_template,
            'description': challenge.description,
            'category': challenge.category,
            'state': challenge.state,
            'max_attempts': challenge.max_attempts,
            'type': challenge.type,
            'type_data': {
                'id': DockerChallengeType.id,
                'name': DockerChallengeType.name,
                'templates': DockerChallengeType.templates,
                'scripts': DockerChallengeType.scripts,
            }
        }
        return data

    @staticmethod
    def create(request):
        """
		This method is used to process the challenge creation request.

		:param request:
		:return:
		"""
        data = request.form or request.get_json()
        data = normalize_challenge_payload(data)
        data = {k: v for k, v in data.items() if k not in ('nonce', 'csrf_token')}
        challenge = DockerChallenge(**data)
        db.session.add(challenge)
        db.session.commit()
        return challenge

    @staticmethod
    def attempt(challenge, request):
        """
		This method is used to check whether a given input is right or wrong. It does not make any changes and should
		return a boolean for correctness and a string to be shown to the user. It is also in charge of parsing the
		user's input from the request itself.

		:param challenge: The Challenge object from the database
		:param request: The request the user submitted
		:return: (boolean, string)
        """

        data = request.form or request.get_json()
        submission = data["submission"].strip()
        if (challenge.flag_mode or "static") == "hmac":
            participant = get_participant(required=False)
            if participant is None:
                return False, "Join a team before submitting this challenge"
            try:
                expected = generate_hmac_flag(challenge, participant)
                if hmac.compare_digest(submission, expected):
                    return True, "Correct"
            except Exception:
                traceback.print_exc()
                return False, "This challenge is misconfigured"
            return False, "Incorrect"
        flags = Flags.query.filter_by(challenge_id=challenge.id).all()
        for flag in flags:
            if get_flag_class(flag.type).compare(flag, submission):
                return True, "Correct"
        return False, "Incorrect"

    @staticmethod
    def solve(user, team, challenge, request):
        """
		This method is used to insert Solves into the database in order to mark a challenge as solved.

		:param team: The Team object from the database
		:param chal: The Challenge object from the database
		:param request: The request the user submitted
		:return:
		"""
        data = request.form or request.get_json()
        submission = data["submission"].strip()
        docker = get_docker_config()
        try:
            participant = team if is_teams_mode() else user
            docker_containers = get_trackers_for_challenge(participant, challenge)
            if docker_containers:
                delete_trackers(
                    docker,
                    docker_containers,
                    reason="solve_cleanup",
                    participant=participant,
                    actor_role="participant",
                    actor_id=participant.id,
                    actor_name=get_participant_name(participant),
                )
        except Exception:
            traceback.print_exc()
        solve = Solves(
            user_id=user.id,
            team_id=team.id if team else None,
            challenge_id=challenge.id,
            ip=get_current_request_ip(),
            provided=submission,
        )
        db.session.add(solve)
        db.session.commit()
        # trying if this solces the detached instance error...
        #db.session.close()

    @staticmethod
    def fail(user, team, challenge, request):
        """
		This method is used to insert Fails into the database in order to mark an answer incorrect.

		:param team: The Team object from the database
		:param chal: The Challenge object from the database
		:param request: The request the user submitted
		:return:
		"""
        data = request.form or request.get_json()
        submission = data["submission"].strip()
        wrong = Fails(
            user_id=user.id,
            team_id=team.id if team else None,
            challenge_id=challenge.id,
            ip=get_current_request_ip(),
            provided=submission,
        )
        db.session.add(wrong)
        db.session.commit()
        #db.session.close()


class DockerChallenge(Challenges):
    __mapper_args__ = {'polymorphic_identity': 'docker'}
    id = db.Column(None, db.ForeignKey('challenges.id'), primary_key=True)
    docker_image = db.Column(db.String(255), index=True)
    compose_content = db.Column(db.Text, nullable=True)
    published_ports = db.Column(db.Text, nullable=True)
    flag_mode = db.Column(db.String(32), nullable=True, default='static')
    flag_template = db.Column(db.String(255), nullable=True)


def cleanup_created_resources(docker, stack_result=None, container_result=None):
    """Remove Docker resources that were created before tracker persistence failed."""
    success = True
    if stack_result:
        for container in reversed(stack_result.get('containers') or []):
            if not delete_container(docker, container.get('instance_id')):
                success = False
        network_id = stack_result.get('network_id')
        if network_id and not delete_network(docker, network_id):
            success = False
    if container_result and not delete_container(docker, container_result.get('Id')):
        success = False
    return success


def mark_instance_failed(participant, challenge, error):
    try:
        db.session.rollback()
        acquire_owner_lock(participant)
        record = get_instance_record(participant, challenge, for_update=True)
        if record is not None:
            update_instance_state(record, INSTANCE_FAILED, error=error, commit=False)
        db.session.commit()
    except Exception:
        traceback.print_exc()
        db.session.rollback()


def reserve_instance_creation(participant, challenge, trackers, docker):
    """Atomically reserve this owner's challenge slot and return an operation token."""
    acquire_owner_lock(participant)
    record = ensure_legacy_instance_record(participant, challenge, trackers)
    now = unix_time(datetime.utcnow())

    if record is not None and record.state in {INSTANCE_CREATING, INSTANCE_DELETING}:
        db.session.rollback()
        abort(409, "A Docker lifecycle operation is already in progress for this challenge")

    needs_revert = bool(trackers)
    if needs_revert:
        valid_timestamps = []
        for tracker in trackers:
            try:
                valid_timestamps.append(int(tracker.timestamp))
            except (TypeError, ValueError):
                pass
        oldest_timestamp = min(valid_timestamps) if valid_timestamps else now
        cooldown = get_revert_cooldown(docker)
        remaining = cooldown - (now - oldest_timestamp)
        if remaining > 0:
            db.session.rollback()
            abort(
                403,
                f"To prevent abuse, containers for this challenge can only be reverted every {cooldown} seconds. "
                f"Please wait {remaining} more seconds.",
            )
        operation_token = secrets.token_hex(16)
        update_instance_state(
            record,
            INSTANCE_DELETING,
            operation_token=operation_token,
            commit=False,
        )
        db.session.commit()
        return record, operation_token, True

    max_active = get_max_active_challenges(docker)
    if max_active:
        active_keys = {
            f"id:{row[0]}"
            for row in db.session.query(DockerChallengeInstance.challenge_id).filter(
                DockerChallengeInstance.owner_mode == get_owner_mode(),
                DockerChallengeInstance.owner_id == str(participant.id),
                DockerChallengeInstance.state.in_(ACTIVE_INSTANCE_STATES),
            ).all()
        }
        for tracker in DockerChallengeTracker.query.filter(build_owner_filter(participant)).all():
            active_keys.add(
                f"id:{tracker.challenge_id}"
                if tracker.challenge_id is not None
                else f"name:{tracker.challenge}"
            )
        if f"id:{challenge.id}" not in active_keys and len(active_keys) >= max_active:
            db.session.rollback()
            abort(
                403,
                f"You already have {max_active} active Docker challenge instance(s). "
                "Stop an existing instance before starting another.",
            )

    operation_token = secrets.token_hex(16)
    if record is None:
        record = DockerChallengeInstance(
            owner_mode=get_owner_mode(),
            owner_id=str(participant.id),
            challenge_id=challenge.id,
            challenge_name=challenge.name,
            instance_key=get_instance_key(participant=participant, challenge=challenge),
            state=INSTANCE_CREATING,
            operation_token=operation_token,
            created_at=now,
            updated_at=now,
        )
        db.session.add(record)
    else:
        record.challenge_name = challenge.name
        record.created_at = now
        update_instance_state(
            record,
            INSTANCE_CREATING,
            operation_token=operation_token,
            commit=False,
        )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, "A Docker lifecycle operation is already in progress for this challenge")
    return record, operation_token, False


def transition_reserved_instance_to_creating(participant, challenge):
    acquire_owner_lock(participant)
    record = get_instance_record(participant, challenge, for_update=True)
    if record is None:
        db.session.rollback()
        raise RuntimeError("Docker lifecycle reservation disappeared")
    operation_token = secrets.token_hex(16)
    record.created_at = unix_time(datetime.utcnow())
    update_instance_state(
        record,
        INSTANCE_CREATING,
        operation_token=operation_token,
        commit=False,
    )
    db.session.commit()
    return record, operation_token


# API
container_namespace = Namespace("container", description='Endpoint to interact with containers')


@container_namespace.route("", methods=['POST'])
class ContainerAPI(Resource):
    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    def post(self):
        payload = request.get_json(silent=True) or request.args
        challenge_id = payload.get('challenge_id')
        challenge_name = payload.get('challenge')
        stop_container = str(payload.get('stopcontainer', '')).lower() in {'1', 'true', 'yes'}

        if challenge_id in (None, '') and not challenge_name:
            return abort(403, "No challenge specified")

        docker = get_docker_config()
        if not docker:
            return abort(503, "Docker has not been configured by an administrator")

        docker_chal = None
        if challenge_id not in (None, ''):
            try:
                docker_chal = DockerChallenge.query.filter_by(id=int(challenge_id)).first()
            except (TypeError, ValueError):
                return abort(400, "Invalid challenge identifier")
        if docker_chal is None and challenge_name:
            docker_chal = DockerChallenge.query.filter_by(name=challenge_name).first()
        if docker_chal is None:
            return abort(404, "Docker challenge not found")
        if docker_chal.state == "hidden":
            return abort(403, "This challenge is not currently available")

        participant = get_participant(required=True)
        cleanup_expired_trackers(docker, participant)

        existing = get_trackers_for_challenge(participant, docker_chal)
        if stop_container:
            acquire_owner_lock(participant)
            record = ensure_legacy_instance_record(participant, docker_chal, existing)
            stop_token = secrets.token_hex(16)
            if record is not None:
                update_instance_state(
                    record,
                    INSTANCE_DELETING,
                    operation_token=stop_token,
                    commit=False,
                )
            db.session.commit()
            if existing:
                failed = delete_trackers(
                    docker,
                    existing,
                    reason="stop",
                    participant=participant,
                    actor_role="participant",
                    actor_id=participant.id,
                    actor_name=get_participant_name(participant),
                )
                if failed:
                    return abort(500, "Failed to stop one or more Docker instances")
            else:
                acquire_owner_lock(participant)
                record = get_instance_record(participant, docker_chal, for_update=True)
                if record is not None:
                    update_instance_state(record, INSTANCE_STOPPED, commit=False)
                db.session.commit()
            return {"success": True, "result": "Container stopped"}

        record, operation_token, needs_revert = reserve_instance_creation(
            participant,
            docker_chal,
            existing,
            docker,
        )
        if needs_revert:
            failed = delete_trackers(
                docker,
                existing,
                reason="revert",
                participant=participant,
                actor_role="participant",
                actor_id=participant.id,
                actor_name=get_participant_name(participant),
            )
            if failed:
                return abort(500, "Failed to remove the existing Docker instance before revert")
            record, operation_token = transition_reserved_instance_to_creating(participant, docker_chal)

        portsbl = get_unavailable_ports(docker)
        now = unix_time(datetime.utcnow())
        host = get_connection_host_value(docker)
        owner_kwargs = get_owner_kwargs(participant)
        challenge_label = docker_chal.name
        instance_key = record.instance_key
        common_labels = {
            'ctfd.plugin': PLUGIN_LABEL,
            'ctfd.challenge.id': str(docker_chal.id),
            'ctfd.challenge.name': challenge_label,
            'ctfd.owner.id': str(participant.id),
            'ctfd.owner.mode': 'teams' if is_teams_mode() else 'users',
            'ctfd.instance.key': instance_key,
            'ctfd.operation.token': operation_token,
        }
        try:
            runtime_env = build_runtime_env(docker_chal, participant)
        except Exception as exc:
            traceback.print_exc()
            log_audit_event(
                "start",
                status="error",
                challenge=docker_chal,
                participant=participant,
                actor_role="participant",
                actor_id=participant.id,
                actor_name=get_participant_name(participant),
                message=f"Failed to build runtime environment: {exc}",
                ip_address=get_current_request_ip(),
            )
            mark_instance_failed(participant, docker_chal, exc)
            return abort(500, f"Failed to prepare the challenge runtime: {exc}")

        stack_result = None
        container_result = None
        if docker_chal.compose_content:
            try:
                stack_result = create_stack(
                    docker,
                    docker_chal.compose_content,
                    instance_key,
                    docker_chal.id,
                    portsbl,
                    labels=common_labels,
                    runtime_env=runtime_env,
                )
            except Exception as exc:
                traceback.print_exc()
                log_audit_event(
                    "start",
                    status="error",
                    challenge=docker_chal,
                    participant=participant,
                    actor_role="participant",
                    actor_id=participant.id,
                    actor_name=get_participant_name(participant),
                    message=f"Failed to create stack: {exc}",
                    ip_address=get_current_request_ip(),
                )
                mark_instance_failed(participant, docker_chal, exc)
                return abort(500, f"Failed to create stack: {exc}")

        else:
            try:
                container_result = create_container(
                    docker,
                    docker_chal.docker_image,
                    instance_key,
                    docker_chal.id,
                    portsbl,
                    labels=common_labels,
                    env_vars=runtime_env,
                    selected_ports=decode_published_ports(docker_chal),
                )
            except Exception as exc:
                traceback.print_exc()
                log_audit_event(
                    "start",
                    status="error",
                    challenge=docker_chal,
                    participant=participant,
                    actor_role="participant",
                    actor_id=participant.id,
                    actor_name=get_participant_name(participant),
                    message=f"Failed to create container: {exc}",
                    ip_address=get_current_request_ip(),
                )
                mark_instance_failed(participant, docker_chal, exc)
                return abort(500, f"Failed to create container: {exc}")

        try:
            acquire_owner_lock(participant)
            locked_record = get_instance_record(participant, docker_chal, for_update=True)
            if (
                locked_record is None
                or locked_record.state != INSTANCE_CREATING
                or locked_record.operation_token != operation_token
            ):
                db.session.rollback()
                cleanup_created_resources(
                    docker,
                    stack_result=stack_result,
                    container_result=container_result,
                )
                return abort(409, "The Docker lifecycle operation was superseded")

            if stack_result:
                for container in stack_result['containers']:
                    ports_string, ports_json = encode_ports(
                        container['ports'].split(',') if container['ports'] else []
                    )
                    db.session.add(DockerChallengeTracker(
                        **owner_kwargs,
                        docker_image=container['image'],
                        timestamp=now,
                        revert_time=now + get_revert_cooldown(docker),
                        instance_id=container['instance_id'],
                        ports=ports_string,
                        ports_json=ports_json,
                        host=host,
                        challenge=challenge_label,
                        challenge_id=docker_chal.id,
                        service_name=container['service_name'],
                        stack_id=stack_result['stack_id'],
                        network_id=stack_result['network_id'],
                        instance_key=instance_key,
                    ))
            else:
                ports_string, ports_json = encode_ports(container_result['ports'])
                db.session.add(DockerChallengeTracker(
                    **owner_kwargs,
                    docker_image=docker_chal.docker_image,
                    timestamp=now,
                    revert_time=now + get_revert_cooldown(docker),
                    instance_id=container_result['Id'],
                    ports=ports_string,
                    ports_json=ports_json,
                    host=host,
                    challenge=challenge_label,
                    challenge_id=docker_chal.id,
                    service_name='primary',
                    instance_key=instance_key,
                ))

            update_instance_state(locked_record, INSTANCE_RUNNING, operation_token=operation_token, commit=False)
            db.session.commit()
        except HTTPException:
            raise
        except Exception as exc:
            traceback.print_exc()
            db.session.rollback()
            cleanup_created_resources(
                docker,
                stack_result=stack_result,
                container_result=container_result,
            )
            mark_instance_failed(participant, docker_chal, exc)
            return abort(500, "The Docker instance started but could not be recorded; it was rolled back")

        entries = DockerChallengeTracker.query.filter_by(instance_key=instance_key).all()
        for entry in entries:
            log_audit_event(
                "start",
                challenge=docker_chal,
                tracker=entry,
                participant=participant,
                actor_role="participant",
                actor_id=participant.id,
                actor_name=get_participant_name(participant),
                message="Started compose stack instance" if stack_result else "Started single-container instance",
                ip_address=get_current_request_ip(),
            )

        return {"success": True, "challenge_id": docker_chal.id}


active_docker_namespace = Namespace("docker_status", description='Endpoint to retrieve User Docker Image Status')


@active_docker_namespace.route("", methods=['POST', 'GET'])
class DockerStatus(Resource):
    """
	The Purpose of this API is to retrieve a public JSON string of all docker containers
	in use by the current team/user.
	"""

    @authed_only
    @during_ctf_time_only
    @require_verified_emails
    def get(self):
        docker = get_docker_config()
        participant = get_participant(required=False)
        ttl = get_container_ttl(docker)
        if participant is None:
            return {
                'success': True,
                'data': [],
                'settings': {
                    'revert_cooldown': get_revert_cooldown(docker),
                    'container_ttl': ttl,
                }
            }
        cleanup_expired_trackers(docker, participant)

        tracker_query = DockerChallengeTracker.query.filter(build_owner_filter(participant)).order_by(
            DockerChallengeTracker.timestamp.desc()
        )
        tracker_entries = tracker_query.all()
        instance_states = {
            record.challenge_id: record.state
            for record in DockerChallengeInstance.query.filter_by(
                owner_mode=get_owner_mode(),
                owner_id=str(participant.id),
            ).all()
        }
        data = []
        seen_stacks = set()
        for i in tracker_entries:
            entry_host = i.host or get_connection_host_value(docker)
            if i.stack_id:
                if i.stack_id in seen_stacks:
                    continue
                seen_stacks.add(i.stack_id)
                # Aggregate all ports from all services in this stack
                stack_query = DockerChallengeTracker.query.filter_by(stack_id=i.stack_id)
                if i.instance_key:
                    stack_query = stack_query.filter_by(instance_key=i.instance_key)
                stack_entries = stack_query.order_by(DockerChallengeTracker.service_name.asc()).all()
                all_ports = []
                services = []
                for entry in stack_entries:
                    service_ports = decode_ports(entry)
                    if service_ports:
                        all_ports.extend(service_ports)
                    services.append({
                        'service_name': entry.service_name or entry.docker_image,
                        'image': entry.docker_image,
                        'ports': service_ports,
                    })
                data.append({
                    'id': i.id,
                    'team_id': i.team_id,
                    'user_id': i.user_id,
                    'docker_image': i.docker_image,
                    'challenge': i.challenge,
                    'challenge_id': i.challenge_id,
                    'state': instance_states.get(i.challenge_id, INSTANCE_RUNNING),
                    'timestamp': i.timestamp,
                    'revert_time': i.revert_time,
                    'expires_at': i.timestamp + ttl,
                    'instance_id': i.instance_id,
                    'ports': all_ports,
                    'host': entry_host,
                    'is_compose': True,
                    'stack_id': i.stack_id,
                    'services': services,
                })
            else:
                service_ports = decode_ports(i)
                data.append({
                    'id': i.id,
                    'team_id': i.team_id,
                    'user_id': i.user_id,
                    'docker_image': i.docker_image,
                    'challenge': i.challenge,
                    'challenge_id': i.challenge_id,
                    'state': instance_states.get(i.challenge_id, INSTANCE_RUNNING),
                    'timestamp': i.timestamp,
                    'revert_time': i.revert_time,
                    'expires_at': i.timestamp + ttl,
                    'instance_id': i.instance_id,
                    'ports': service_ports,
                    'host': entry_host,
                    'service_name': i.service_name or 'primary',
                    'services': [{
                        'service_name': i.service_name or 'primary',
                        'image': i.docker_image,
                        'ports': service_ports,
                    }],
                })
        return {
            'success': True,
            'data': data,
            'settings': {
                'revert_cooldown': get_revert_cooldown(docker),
                'container_ttl': ttl,
            }
        }


docker_namespace = Namespace("docker", description='Endpoint to retrieve dockerstuff')


@docker_namespace.route("", methods=['POST', 'GET'])
class DockerAPI(Resource):
    """
	This is for creating Docker Challenges. The purpose of this API is to populate the Docker Image Select form
	object in the Challenge Creation Screen.
	"""

    @admins_only
    def get(self):
        docker = get_docker_config()
        if not docker:
            return {
                'success': False,
                'message': 'Configure Docker access first',
                'data': [{'name': 'Error in Docker Config!'}],
            }, 400

        images = get_repositories(docker, tags=True, repos=docker.repositories)
        if images:
            requested_image = (request.args.get('image') or '').strip()
            if requested_image:
                if requested_image not in images:
                    return {"success": False, "message": "Image is not currently allowed"}, 404
                try:
                    ports = get_required_ports(docker, requested_image)
                except RuntimeError as exc:
                    return {"success": False, "message": str(exc)}, 400
                return {
                    "success": True,
                    "data": {"name": requested_image, "ports": ports},
                }
            data = list()
            for i in images:
                data.append({'name': i})
            return {
                'success': True,
                'data': data
            }
        else:
            return {
                       'message': 'Docker API request failed or no repositories are currently allow-listed',
                       'success': False,
                       'data': [
                           {
                               'name': 'Error in Docker Config!'
                           }
                       ]
                   }, 400



def _ensure_columns(app):
    """Add new columns to existing tables if they don't exist (simple migration)."""
    with app.app_context():
        from sqlalchemy import inspect, text
        engine = db.engine
        insp = inspect(engine)
        dialect = engine.dialect.name
        quote_identifier = engine.dialect.identifier_preparer.quote

        def ensure_index(table_name, index_name, column_name):
            existing = {item['name'] for item in inspect(engine).get_indexes(table_name)}
            if index_name in existing:
                return
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f"CREATE INDEX {quote_identifier(index_name)} ON "
                        f"{quote_identifier(table_name)} ({quote_identifier(column_name)})"
                    ))
            except Exception:
                refreshed = {item['name'] for item in inspect(engine).get_indexes(table_name)}
                if index_name not in refreshed:
                    raise

        # Older versions indexed certificate bodies and the repository CSV.
        # Drop those indexes before widening to TEXT; large text values are not
        # valid portable B-tree keys on MySQL/PostgreSQL.
        if insp.has_table('docker_config'):
            oversized_columns = {'ca_cert', 'client_cert', 'client_key', 'repositories'}
            with engine.begin() as conn:
                for index in insp.get_indexes('docker_config'):
                    if not oversized_columns.intersection(index.get('column_names') or []):
                        continue
                    index_name = quote_identifier(index['name'])
                    table_name = quote_identifier('docker_config')
                    if dialect in {'mysql', 'mariadb'}:
                        conn.execute(text(f'DROP INDEX {index_name} ON {table_name}'))
                    else:
                        conn.execute(text(f'DROP INDEX IF EXISTS {index_name}'))

        # DockerConfig columns
        config_cols = {c['name'] for c in insp.get_columns('docker_config')} if insp.has_table('docker_config') else set()
        if 'docker_config' in insp.get_table_names():
            with engine.begin() as conn:
                if 'revert_cooldown' not in config_cols:
                    conn.execute(text('ALTER TABLE docker_config ADD COLUMN revert_cooldown INTEGER'))
                if 'container_ttl' not in config_cols:
                    conn.execute(text('ALTER TABLE docker_config ADD COLUMN container_ttl INTEGER'))
                if 'max_active' not in config_cols:
                    conn.execute(text('ALTER TABLE docker_config ADD COLUMN max_active INTEGER'))
                if 'reaper_last_run' not in config_cols:
                    conn.execute(text('ALTER TABLE docker_config ADD COLUMN reaper_last_run INTEGER'))
                if 'reaper_lock_until' not in config_cols:
                    conn.execute(text('ALTER TABLE docker_config ADD COLUMN reaper_lock_until INTEGER'))

        # DockerChallengeTracker columns
        tracker_cols = {c['name'] for c in insp.get_columns('docker_challenge_tracker')} if insp.has_table('docker_challenge_tracker') else set()
        if 'docker_challenge_tracker' in insp.get_table_names():
            with engine.begin() as conn:
                if 'challenge_id' not in tracker_cols:
                    conn.execute(text('ALTER TABLE docker_challenge_tracker ADD COLUMN challenge_id INTEGER'))
                if 'service_name' not in tracker_cols:
                    conn.execute(text('ALTER TABLE docker_challenge_tracker ADD COLUMN service_name VARCHAR(128)'))
                if 'stack_id' not in tracker_cols:
                    conn.execute(text('ALTER TABLE docker_challenge_tracker ADD COLUMN stack_id VARCHAR(64)'))
                if 'network_id' not in tracker_cols:
                    conn.execute(text('ALTER TABLE docker_challenge_tracker ADD COLUMN network_id VARCHAR(128)'))
                if 'ports_json' not in tracker_cols:
                    conn.execute(text('ALTER TABLE docker_challenge_tracker ADD COLUMN ports_json TEXT'))
                if 'instance_key' not in tracker_cols:
                    conn.execute(text('ALTER TABLE docker_challenge_tracker ADD COLUMN instance_key VARCHAR(160)'))
            ensure_index('docker_challenge_tracker', 'ix_docker_challenge_tracker_challenge_id', 'challenge_id')
            ensure_index('docker_challenge_tracker', 'ix_docker_challenge_tracker_service_name', 'service_name')
            ensure_index('docker_challenge_tracker', 'ix_docker_challenge_tracker_instance_key', 'instance_key')

        # DockerChallenge columns (child table via joined inheritance)
        dc_cols = {c['name'] for c in insp.get_columns('docker_challenge')} if insp.has_table('docker_challenge') else set()
        if 'docker_challenge' in insp.get_table_names():
            with engine.begin() as conn:
                if 'compose_content' not in dc_cols:
                    conn.execute(text('ALTER TABLE docker_challenge ADD COLUMN compose_content TEXT'))
                if 'flag_mode' not in dc_cols:
                    conn.execute(text("ALTER TABLE docker_challenge ADD COLUMN flag_mode VARCHAR(32)"))
                if 'flag_template' not in dc_cols:
                    conn.execute(text("ALTER TABLE docker_challenge ADD COLUMN flag_template VARCHAR(255)"))
                if 'published_ports' not in dc_cols:
                    conn.execute(text("ALTER TABLE docker_challenge ADD COLUMN published_ports TEXT"))

        if insp.has_table('docker_config') and dialect != 'sqlite':
            refreshed_config_columns = {
                column['name']: column
                for column in inspect(engine).get_columns('docker_config')
            }
            hostname_type = refreshed_config_columns.get('hostname', {}).get('type')
            hostname_length = getattr(hostname_type, 'length', None)
            text_columns_to_widen = [
                column_name
                for column_name in ('ca_cert', 'client_cert', 'client_key', 'repositories')
                if 'TEXT' not in str(refreshed_config_columns.get(column_name, {}).get('type', '')).upper()
            ]
            with engine.begin() as conn:
                if dialect in {'mysql', 'mariadb'}:
                    if hostname_length is not None and hostname_length < 255:
                        conn.execute(text('ALTER TABLE docker_config MODIFY hostname VARCHAR(255)'))
                    for column_name in text_columns_to_widen:
                        conn.execute(text(
                            f'ALTER TABLE docker_config MODIFY {quote_identifier(column_name)} TEXT'
                        ))
                elif dialect == 'postgresql':
                    if hostname_length is not None and hostname_length < 255:
                        conn.execute(text('ALTER TABLE docker_config ALTER COLUMN hostname TYPE VARCHAR(255)'))
                    for column_name in text_columns_to_widen:
                        conn.execute(text(
                            f'ALTER TABLE docker_config ALTER COLUMN {quote_identifier(column_name)} TYPE TEXT'
                        ))

        if 'docker_audit_log' in insp.get_table_names():
            ensure_index('docker_audit_log', 'ix_docker_audit_log_timestamp', 'timestamp')
            ensure_index('docker_audit_log', 'ix_docker_audit_log_challenge_id', 'challenge_id')


def load(app):
    app.db.create_all()
    _ensure_columns(app)
    with app.app_context():
        run_reaper_cycle()
    CHALLENGE_CLASSES['docker'] = DockerChallengeType
    @app.template_filter('datetimeformat')
    def datetimeformat(value, format='%Y-%m-%d %H:%M:%S'):
        return datetime.fromtimestamp(value).strftime(format)
    register_plugin_assets_directory(app, base_path='/plugins/docker_challenges/assets')
    define_docker_admin(app)
    define_docker_status(app)
    CTFd_API_v1.add_namespace(docker_namespace, '/docker')
    CTFd_API_v1.add_namespace(container_namespace, '/container')
    CTFd_API_v1.add_namespace(active_docker_namespace, '/docker_status')
    CTFd_API_v1.add_namespace(kill_container, '/nuke')
    start_reaper_thread(app)
