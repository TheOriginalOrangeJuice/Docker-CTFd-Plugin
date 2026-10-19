from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest
from CTFd.models import Solves, Teams, Users, db
from CTFd.utils import set_config
from CTFd.utils.security.signing import hmac

from .conftest import plugin
from .fake_docker import FakeDocker, Response
from .test_remote_runtime import SINGLE, STACK


@pytest.fixture
def engine(monkeypatch):
    return FakeDocker().install(monkeypatch, plugin)


@pytest.fixture
def saved(app):
    with app.app_context():
        user = Users(name="player", email="player@example.invalid", password="test-password", verified=True)
        other = Users(name="other", email="other@example.invalid", password="test-password", verified=True)
        config = plugin.DockerConfig(id=1, hostname="ctf-docker.utsacyber.local:2376", tls_enabled=True,
                                     repositories="utsa/web,utsa/db", revert_cooldown=300, container_ttl=7200, max_active=0)
        challenge = plugin.DockerChallenge(name="Docker test", category="test", description="test", value=100,
                                           state="visible", docker_image=SINGLE, flag_mode="hmac", flag_template="flag{{HMAC}}")
        db.session.add_all([user, other, config, challenge])
        db.session.commit()
        return SimpleNamespace(user_id=user.id, other_id=other.id, challenge_id=challenge.id)


def client_for(app, user_id):
    client = app.test_client()
    with app.app_context():
        user = Users.query.get(user_id)
        password_hash = hmac(user.password)
    with client.session_transaction() as session:
        session["id"] = user_id
        session["hash"] = password_hash
        session["nonce"] = "test-csrf-token"
    return client


def launch(client, challenge_id, **data):
    return client.post("/test/container", json={"challenge_id": challenge_id, **data}, headers={"CSRF-Token": "test-csrf-token"})


@pytest.mark.parametrize("stack", [False, True])
def test_participant_round_trip_tracking_ports_status_and_stop(app, saved, engine, stack):
    if stack:
        with app.app_context():
            plugin.DockerChallenge.query.get(saved.challenge_id).compose_content = STACK
            db.session.commit()
    client = client_for(app, saved.user_id)
    result = launch(client, saved.challenge_id)
    assert result.status_code == 200, result.get_data(as_text=True)
    with app.app_context():
        record = plugin.DockerChallengeInstance.query.one()
        assert record.owner_id == str(saved.user_id)
        assert record.state == plugin.INSTANCE_RUNNING
        trackers = plugin.DockerChallengeTracker.query.all()
        assert len(trackers) == (2 if stack else 1)
        assert all(t.user_id == str(saved.user_id) and t.instance_key == record.instance_key for t in trackers)
        assert all(t.host == "172.93.163.183" and t.revert_time == t.timestamp + 300 for t in trackers)
        expected_flag = plugin.generate_hmac_flag(plugin.DockerChallenge.query.get(saved.challenge_id), Users.query.get(saved.user_id))
    creates = [data for _, url, data in engine.calls if url.startswith("/containers/create")]
    assert all("CTFD_FLAG=" + expected_flag in c["Env"] for c in creates)
    status = client.get("/test/status").get_json()
    assert len(status["data"]) == 1
    assert status["data"][0]["challenge_id"] == saved.challenge_id
    assert status["data"][0]["expires_at"] == status["data"][0]["timestamp"] + 7200
    assert status["data"][0]["ports"]
    assert client_for(app, saved.other_id).get("/test/status").get_json()["data"] == []
    result = launch(client, saved.challenge_id, stopcontainer=True)
    assert result.status_code == 200
    assert engine.containers == engine.networks == {}
    with app.app_context():
        assert plugin.DockerChallengeTracker.query.count() == 0
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_STOPPED


@pytest.mark.parametrize("override", [{"image": "bad:latest"}, {"docker_image": "bad:latest"}, {"HostConfig": {"Privileged": True}}, {"environment": {"CTFD_FLAG": "bad"}}, {"ports": [2376]}, {"compose_content": STACK}, {"owner_id": "other"}])
def test_participant_cannot_override_saved_runtime(app, saved, engine, override):
    result = launch(client_for(app, saved.user_id), saved.challenge_id, **override)
    assert result.status_code == 400
    assert engine.calls == []
    with app.app_context():
        assert plugin.DockerChallengeInstance.query.count() == 0


def test_legacy_client_image_name_is_not_an_image_selector(app, saved, engine):
    result = launch(client_for(app, saved.user_id), saved.challenge_id, name="unapproved/image:latest")
    assert result.status_code == 200
    creates = [data for _, url, data in engine.calls if url.startswith("/containers/create")]
    assert creates[0]["Image"] == engine.image_id


@pytest.mark.parametrize("body", [[], "bad", True, 12])
def test_non_object_launch_payload_rejected(app, saved, engine, body):
    client = client_for(app, saved.user_id)
    result = client.post("/test/container", json=body, headers={"CSRF-Token": "test-csrf-token"})
    assert result.status_code == 400
    assert engine.calls == []


def test_remote_failure_marks_reservation_failed_without_orphaned_creating_state(app, saved, engine, monkeypatch):
    original = engine.request
    def fail_list(docker, url, **kwargs):
        return Response([], 503, "remote worker unavailable") if url == "/containers/json?all=1" else original(docker, url, **kwargs)
    monkeypatch.setattr(plugin, "do_request", fail_list)
    result = launch(client_for(app, saved.user_id), saved.challenge_id)
    assert result.status_code == 500
    with app.app_context():
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_FAILED
        assert plugin.DockerChallengeTracker.query.count() == 0
    assert engine.containers == engine.networks == {}


@pytest.mark.parametrize('stack', [False, True])
def test_cooldown_is_preserved_and_revert_removes_old_resources(app, saved, engine, stack):
    if stack:
        with app.app_context():
            plugin.DockerChallenge.query.get(saved.challenge_id).compose_content = STACK
            db.session.commit()
    client = client_for(app, saved.user_id)
    assert launch(client, saved.challenge_id).status_code == 200
    original_id = next(iter(engine.containers))
    original_network = next(iter(engine.networks))
    assert launch(client, saved.challenge_id).status_code == 403
    assert original_id in engine.containers and len(engine.containers) == (2 if stack else 1)
    with app.app_context():
        for tracker in plugin.DockerChallengeTracker.query.all():
            tracker.timestamp -= 301
        db.session.commit()
    assert launch(client, saved.challenge_id).status_code == 200
    assert len(engine.containers) == (2 if stack else 1) and original_id not in engine.containers
    assert original_network not in engine.networks and len(engine.networks) == 1


@pytest.mark.parametrize("stack", [False, True])
def test_ttl_cleanup_only_removes_expired_owner(app, saved, engine, stack):
    if stack:
        with app.app_context():
            plugin.DockerChallenge.query.get(saved.challenge_id).compose_content = STACK
            db.session.commit()
    assert launch(client_for(app, saved.user_id), saved.challenge_id).status_code == 200
    assert launch(client_for(app, saved.other_id), saved.challenge_id).status_code == 200
    with app.app_context():
        for tracker in plugin.DockerChallengeTracker.query.filter_by(user_id=str(saved.user_id)):
            tracker.timestamp -= 7201
        db.session.commit()
        plugin.cleanup_all_expired_trackers(plugin.get_docker_config())
        remaining = plugin.DockerChallengeTracker.query.all()
        assert len(remaining) == (2 if stack else 1)
        assert all(t.user_id == str(saved.other_id) for t in remaining)
        records = {r.owner_id: r.state for r in plugin.DockerChallengeInstance.query.all()}
        assert records[str(saved.user_id)] == plugin.INSTANCE_STOPPED
        assert records[str(saved.other_id)] == plugin.INSTANCE_RUNNING
    assert len(engine.containers) == (2 if stack else 1)
    assert len(engine.networks) == 1


@pytest.mark.parametrize('stack', [False, True])
def test_solve_cleanup_preserves_score_and_other_participant(app, saved, engine, stack):
    if stack:
        with app.app_context():
            plugin.DockerChallenge.query.get(saved.challenge_id).compose_content = STACK
            db.session.commit()
    assert launch(client_for(app, saved.user_id), saved.challenge_id).status_code == 200
    assert launch(client_for(app, saved.other_id), saved.challenge_id).status_code == 200
    with app.app_context():
        db.session.add(Solves(user_id=saved.user_id, challenge_id=saved.challenge_id, provided="test"))
        db.session.commit()
        plugin.cleanup_solved_trackers(plugin.get_docker_config())
        assert Solves.query.count() == 1
        remaining = plugin.DockerChallengeTracker.query.all()
        assert len(remaining) == (2 if stack else 1)
        assert all(t.user_id == str(saved.other_id) for t in remaining)
        assert {t.network_id for t in remaining} == set(engine.networks)
    assert len(engine.containers) == (2 if stack else 1)
    assert len(engine.networks) == 1


def test_failed_cleanup_retains_trackers_for_retry(app, saved, engine):
    with app.app_context():
        plugin.DockerChallenge.query.get(saved.challenge_id).compose_content = STACK
        db.session.commit()
    client = client_for(app, saved.user_id)
    assert launch(client, saved.challenge_id).status_code == 200
    engine.delete_failures.add(next(iter(engine.containers)))
    assert launch(client, saved.challenge_id, stopcontainer=True).status_code == 500
    with app.app_context():
        assert plugin.DockerChallengeTracker.query.count() == 2
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_FAILED
    engine.delete_failures.clear()
    assert launch(client, saved.challenge_id, stopcontainer=True).status_code == 200
    assert engine.containers == engine.networks == {}
    with app.app_context():
        assert plugin.DockerChallengeTracker.query.count() == 0
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_STOPPED


def test_concurrent_same_owner_cannot_create_duplicate_instances(app, saved, engine, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    request = engine.request
    def pause_create(docker, url, **kwargs):
        if url.startswith("/containers/create"):
            entered.set()
            assert release.wait(10)
        return request(docker, url, **kwargs)
    monkeypatch.setattr(engine, "request", pause_create)
    first, second = client_for(app, saved.user_id), client_for(app, saved.user_id)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(launch, first, saved.challenge_id)
        try:
            assert entered.wait(10)
            result = launch(second, saved.challenge_id)
            assert result.status_code == 409, result.get_data(as_text=True)
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 200
    assert len(engine.containers) == 1
    with app.app_context():
        assert plugin.DockerChallengeInstance.query.count() == 1
        assert plugin.DockerChallengeTracker.query.count() == 1


def test_team_members_share_one_participant_instance(app, saved, engine):
    with app.app_context():
        set_config("user_mode", "teams")
        team = Teams(name="test-team", email="team@example.invalid", password="password")
        db.session.add(team)
        db.session.flush()
        for user in Users.query:
            user.team_id = team.id
        db.session.commit()
        team_id = team.id
    assert launch(client_for(app, saved.user_id), saved.challenge_id).status_code == 200
    status = client_for(app, saved.other_id).get("/test/status").get_json()
    assert len(status["data"]) == 1
    with app.app_context():
        tracker = plugin.DockerChallengeTracker.query.one()
        assert tracker.team_id == str(team_id) and tracker.user_id is None
        record = plugin.DockerChallengeInstance.query.one()
        assert record.owner_mode == "teams" and record.owner_id == str(team_id)


def test_allowed_repository_change_blocks_previously_saved_challenge(app, saved, engine):
    with app.app_context():
        plugin.DockerConfig.query.one().repositories = "utsa/db"
        db.session.commit()
    result = launch(client_for(app, saved.user_id), saved.challenge_id)
    assert result.status_code == 500
    assert engine.calls == []
    with app.app_context():
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_FAILED


def test_concurrent_different_owners_persist_distinct_ports_and_trackers(app, saved, engine, monkeypatch):
    engine.snapshot_barrier = threading.Barrier(2)
    engine.start_barrier = threading.Barrier(2)
    monkeypatch.setattr(plugin.random, "choice", min)
    clients = [client_for(app, saved.user_id), client_for(app, saved.other_id)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(launch, client, saved.challenge_id) for client in clients]
        results = [f.result(timeout=30) for f in futures]
    assert [r.status_code for r in results] == [200, 200], [r.get_data(as_text=True) for r in results]
    with app.app_context():
        trackers = plugin.DockerChallengeTracker.query.all()
        assert {t.user_id for t in trackers} == {str(saved.user_id), str(saved.other_id)}
        assert {t.ports for t in trackers} == {"30000/tcp", "30001/tcp"}
        assert {r.state for r in plugin.DockerChallengeInstance.query.all()} == {plugin.INSTANCE_RUNNING}
    assert len(engine.containers) == 2


@pytest.mark.parametrize('stack', [False, True])
def test_stop_during_creation_supersedes_launch_and_removes_late_container(app, saved, engine, monkeypatch, stack):
    if stack:
        with app.app_context():
            plugin.DockerChallenge.query.get(saved.challenge_id).compose_content = STACK
            db.session.commit()
    entered, release = threading.Event(), threading.Event()
    request = engine.request
    def pause_create(docker, url, **kwargs):
        if url.startswith("/containers/create"):
            entered.set()
            assert release.wait(10)
        return request(docker, url, **kwargs)
    monkeypatch.setattr(engine, "request", pause_create)
    starting, stopping = client_for(app, saved.user_id), client_for(app, saved.user_id)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(launch, starting, saved.challenge_id)
        try:
            assert entered.wait(10)
            assert launch(stopping, saved.challenge_id, stopcontainer=True).status_code == 200
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 409
    assert engine.containers == engine.networks == {}
    with app.app_context():
        assert plugin.DockerChallengeTracker.query.count() == 0
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_STOPPED


@pytest.mark.parametrize('stack', [False, True])
def test_old_launch_cannot_delete_or_fail_a_newer_successful_instance(app, saved, engine, monkeypatch, stack):
    if stack:
        with app.app_context():
            plugin.DockerChallenge.query.get(saved.challenge_id).compose_content = STACK
            db.session.commit()
    entered, release = threading.Event(), threading.Event()
    request = engine.request
    def pause_first_create(docker, url, **kwargs):
        if url.startswith("/containers/create") and not entered.is_set():
            entered.set()
            assert release.wait(10)
        return request(docker, url, **kwargs)
    monkeypatch.setattr(engine, "request", pause_first_create)
    old, current = client_for(app, saved.user_id), client_for(app, saved.user_id)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(launch, old, saved.challenge_id)
        try:
            assert entered.wait(10)
            assert launch(current, saved.challenge_id, stopcontainer=True).status_code == 200
            assert launch(current, saved.challenge_id).status_code == 200
            new_id = next(iter(engine.containers))
            with app.app_context():
                new_token = plugin.DockerChallengeInstance.query.one().operation_token
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 500  # Old Docker name collision.
    assert new_id in engine.containers and len(engine.containers) == (2 if stack else 1)
    assert len(engine.networks) == 1
    with app.app_context():
        record = plugin.DockerChallengeInstance.query.one()
        assert record.state == plugin.INSTANCE_RUNNING and record.operation_token == new_token
        trackers = plugin.DockerChallengeTracker.query.all()
        assert new_id in {t.instance_id for t in trackers}
        assert {t.network_id for t in trackers} == set(engine.networks)
