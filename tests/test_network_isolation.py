"""Network ownership, per-instance membership, and lifecycle recovery on fake Docker."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest
from CTFd.models import Challenges, db

from .conftest import plugin
from .fake_docker import FakeDocker, Response
from .test_lifecycle import client_for, launch, saved
from .test_remote_runtime import SINGLE, STACK


@pytest.fixture
def engine(monkeypatch):
    return FakeDocker().install(monkeypatch, plugin)


def labels_for(owner_id="1", challenge_id=1, mode="users", token="operation-1"):
    return {
        'ctfd.plugin': plugin.PLUGIN_LABEL,
        'ctfd.owner.mode': mode,
        'ctfd.owner.id': owner_id,
        'ctfd.challenge.id': str(challenge_id),
        'ctfd.instance.key': f'{mode}:{owner_id}:{challenge_id}',
        'ctfd.operation.token': token,
    }


def start(docker_config, stack, labels):
    args = (docker_config, STACK if stack else SINGLE, labels['ctfd.instance.key'],
            int(labels['ctfd.challenge.id']), [])
    return (plugin.create_stack if stack else plugin.create_container)(*args, labels=labels)


def set_mode(app, saved, stack):
    if stack:
        with app.app_context():
            plugin.DockerChallenge.query.get(saved.challenge_id).compose_content = STACK
            db.session.commit()


@pytest.mark.parametrize('stack', [False, True])
@pytest.mark.parametrize('other', [labels_for('2'), labels_for(challenge_id=2), labels_for(mode='teams')])
def test_instances_have_disjoint_networks_and_preserve_service_dns(docker_config, engine, stack, other):
    results = [start(docker_config, stack, labels) for labels in (labels_for(), other)]
    assert len(engine.networks) == 2
    assert results[0]['network_id'] != results[1]['network_id']
    for result, labels in zip(results, (labels_for(), other)):
        network = engine.networks[result['network_id']]
        assert network['Driver'] == 'bridge'
        assert all(network['Labels'][key] == value for key, value in labels.items())
        # Keep Docker's existing NAT/ICC/address-pool defaults; no firewall,
        # Internal, custom subnet, or cross-network attachment overrides.
        assert not {'Internal', 'Options', 'IPAM'} & network.keys()
        ids = [item['instance_id'] for item in result['containers']] if stack else [result['Id']]
        for container_id in ids:
            container = engine.containers[container_id]
            assert container['HostConfig']['NetworkMode'] == result['network_id']
            attachments = container['NetworkSettings']['Networks']
            assert list(attachments) == [network['Name']]
            assert attachments[network['Name']]['NetworkID'] == result['network_id']
            if stack:
                assert attachments[network['Name']]['Aliases'] == [container['Config']['Labels']['ctfd.service.name']]
        assert len(ids) == (2 if stack else 1)


@pytest.mark.parametrize('stack', [False, True])
def test_concurrent_instances_track_separate_networks(app, saved, engine, monkeypatch, stack):
    set_mode(app, saved, stack)
    engine.snapshot_barrier = threading.Barrier(2)
    engine.start_barrier = threading.Barrier(2)
    monkeypatch.setattr(plugin.random, 'choice', min)
    clients = [client_for(app, user_id) for user_id in (saved.user_id, saved.other_id)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda client: launch(client, saved.challenge_id), clients))
    assert [response.status_code for response in results] == [200, 200]
    with app.app_context():
        trackers = plugin.DockerChallengeTracker.query.all()
        for user_id in (saved.user_id, saved.other_id):
            owned = [t for t in trackers if t.user_id == str(user_id)]
            assert len(owned) == (2 if stack else 1)
            assert len({t.network_id for t in owned}) == 1
            assert all(t.network_id in engine.networks for t in owned)
        assert len({t.network_id for t in trackers}) == 2


@pytest.mark.parametrize('stack', [False, True])
@pytest.mark.parametrize('recovery', ['stop', 'reaper'])
def test_network_delete_failure_retains_tracking_and_retries(app, saved, engine, stack, recovery):
    set_mode(app, saved, stack)
    client = client_for(app, saved.user_id)
    assert launch(client, saved.challenge_id).status_code == 200
    network_id = next(iter(engine.networks))
    engine.delete_failures.add(network_id)
    assert launch(client, saved.challenge_id, stopcontainer=True).status_code == 500
    assert engine.containers == {}
    assert network_id in engine.networks
    with app.app_context():
        assert plugin.DockerChallengeTracker.query.count() == (2 if stack else 1)
        record = plugin.DockerChallengeInstance.query.one()
        assert record.state == plugin.INSTANCE_FAILED
        record.updated_at -= plugin.INSTANCE_RECONCILE_GRACE + 1
        db.session.commit()
    engine.delete_failures.clear()
    if recovery == 'stop':
        assert launch(client, saved.challenge_id, stopcontainer=True).status_code == 200
    else:
        with app.app_context():
            assert plugin.reconcile_docker_resources(plugin.get_docker_config())
    assert engine.containers == engine.networks == {}
    with app.app_context():
        assert plugin.DockerChallengeTracker.query.count() == 0
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_STOPPED


@pytest.mark.parametrize('key,value', [
    ('ctfd.plugin', 'another-plugin'), ('ctfd.owner.id', '2'),
    ('ctfd.owner.mode', 'teams'), ('ctfd.challenge.id', '2'),
    ('ctfd.instance.key', 'users:2:1'), ('ctfd.operation.token', 'newer-operation'),
    ('ctfd.stack.id', 'another-stack'),
])
def test_network_deletion_refuses_foreign_instance_or_operation(docker_config, engine, key, value):
    result = start(docker_config, True, labels_for())
    for container in result['containers']:
        assert plugin.delete_container(docker_config, container['instance_id'])
    expected = deepcopy(result['network_labels'])
    engine.networks[result['network_id']]['Labels'][key] = value
    assert not plugin.delete_network(docker_config, result['network_id'], expected)
    assert result['network_id'] in engine.networks
    assert not any(method == 'DELETE' and url.startswith('/networks/') for method, url, _ in engine.calls)


@pytest.mark.parametrize('name', ['bridge', 'host', 'none'])
def test_shared_networks_are_never_deleted_by_name_or_id(docker_config, engine, name):
    labels = labels_for()
    engine.networks['builtin-id'] = {'Id': 'builtin-id', 'Name': name, 'Driver': 'bridge', 'Labels': labels}
    assert not plugin.delete_network(docker_config, name, labels)
    assert not plugin.delete_network(docker_config, 'builtin-id', labels)
    assert 'builtin-id' in engine.networks


def test_name_conflict_does_not_delete_unrelated_endpoint(docker_config, engine):
    result = start(docker_config, False, labels_for())
    own = engine.containers[result['Id']]
    engine.containers['unrelated'] = {
        **deepcopy(own), 'Id': 'unrelated', 'Name': 'unrelated', 'Config': {'Labels': labels_for('2')},
    }
    network_name = engine.networks[result['network_id']]['Name']
    assert not plugin.cleanup_conflicting_network(docker_config, network_name, result['network_labels'])
    assert set(engine.containers) == {result['Id'], 'unrelated'}
    assert result['network_id'] in engine.networks
    assert not any(method == 'DELETE' for method, _, _ in engine.calls)


@pytest.mark.parametrize('stack', [False, True])
@pytest.mark.parametrize('reply', [None, Response({}), Response([])])
def test_lost_network_create_reply_cleans_up_by_owned_name(docker_config, engine, monkeypatch, stack, reply):
    request = engine.request
    def lose_reply(docker, url, **kwargs):
        response = request(docker, url, **kwargs)
        return reply if url == '/networks/create' else response
    monkeypatch.setattr(engine, 'request', lose_reply)
    with pytest.raises(RuntimeError):
        start(docker_config, stack, labels_for())
    assert engine.containers == engine.networks == {}


@pytest.mark.parametrize('reply', [None, Response({}, 500), Response([])])
def test_network_inspection_failure_preserves_tracker_for_retry(app, saved, engine, monkeypatch, reply):
    client = client_for(app, saved.user_id)
    assert launch(client, saved.challenge_id).status_code == 200
    network_id = next(iter(engine.networks))
    request = engine.request
    def fail_inspection(docker, url, **kwargs):
        if url == '/networks/' + network_id and kwargs.get('method', 'GET') == 'GET':
            return reply
        return request(docker, url, **kwargs)
    monkeypatch.setattr(plugin, 'do_request', fail_inspection)
    assert launch(client, saved.challenge_id, stopcontainer=True).status_code == 500
    assert engine.containers == {} and network_id in engine.networks
    with app.app_context():
        assert plugin.DockerChallengeTracker.query.one().network_id == network_id
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_FAILED
    monkeypatch.setattr(plugin, 'do_request', request)
    assert launch(client, saved.challenge_id, stopcontainer=True).status_code == 200
    assert engine.networks == {}


@pytest.mark.parametrize('stack', [False, True])
@pytest.mark.parametrize('failure', ['lost-container-reply', 'failed-container-delete', 'failed-network-delete'])
def test_partial_launch_failure_is_recovered_by_reconciliation(app, saved, engine, monkeypatch, stack, failure):
    set_mode(app, saved, stack)
    request = engine.request
    def fail_launch(docker, url, **kwargs):
        response = request(docker, url, **kwargs)
        if failure == 'lost-container-reply' and url.startswith('/containers/create'):
            return None
        if url.endswith('/start'):
            if failure == 'failed-container-delete':
                engine.delete_failures.add(url.split('/')[2])
            elif failure == 'failed-network-delete':
                engine.delete_failures.update(engine.networks)
            return Response({}, 500, 'injected runtime failure')
        return response
    monkeypatch.setattr(engine, 'request', fail_launch)
    assert launch(client_for(app, saved.user_id), saved.challenge_id).status_code == 500
    assert len(engine.networks) == 1
    with app.app_context():
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_FAILED
        assert plugin.DockerChallengeTracker.query.count() == 0
    engine.delete_failures.clear()
    monkeypatch.setattr(engine, 'request', request)
    with app.app_context():
        assert plugin.reconcile_docker_resources(plugin.get_docker_config())
    assert engine.containers == engine.networks == {}


@pytest.mark.parametrize('stack', [False, True])
def test_persistence_failure_rolls_back_containers_and_network(app, saved, engine, monkeypatch, stack):
    set_mode(app, saved, stack)
    original = plugin.update_instance_state
    def fail_running(record, state, **kwargs):
        if state == plugin.INSTANCE_RUNNING:
            raise RuntimeError('injected tracker commit failure')
        return original(record, state, **kwargs)
    monkeypatch.setattr(plugin, 'update_instance_state', fail_running)
    assert launch(client_for(app, saved.user_id), saved.challenge_id).status_code == 500
    assert engine.containers == engine.networks == {}
    with app.app_context():
        assert plugin.DockerChallengeTracker.query.count() == 0
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_FAILED


@pytest.mark.parametrize('stack', [False, True])
def test_challenge_removal_deletes_each_owners_network(app, saved, engine, stack):
    set_mode(app, saved, stack)
    for user_id in (saved.user_id, saved.other_id):
        assert launch(client_for(app, user_id), saved.challenge_id).status_code == 200
    assert len(engine.networks) == 2
    with app.app_context():
        plugin.DockerChallengeType.delete(plugin.DockerChallenge.query.get(saved.challenge_id))
        assert plugin.DockerChallengeTracker.query.count() == 0
        assert plugin.DockerChallengeInstance.query.count() == 0
        assert Challenges.query.get(saved.challenge_id) is None
    assert engine.containers == engine.networks == {}


@pytest.mark.parametrize('stack', [False, True])
def test_reconcile_missing_container_cleans_its_network_only(app, saved, engine, stack):
    set_mode(app, saved, stack)
    for user_id in (saved.user_id, saved.other_id):
        assert launch(client_for(app, user_id), saved.challenge_id).status_code == 200
    with app.app_context():
        owned = plugin.DockerChallengeTracker.query.filter_by(user_id=str(saved.user_id)).all()
        missing_network = owned[0].network_id
        for tracker in owned:
            del engine.containers[tracker.instance_id]
        assert plugin.reconcile_docker_resources(plugin.get_docker_config())
        assert all(t.user_id == str(saved.other_id) for t in plugin.DockerChallengeTracker.query.all())
    assert missing_network not in engine.networks
    assert len(engine.networks) == 1
    assert len(engine.containers) == (2 if stack else 1)


def test_reaper_preserves_recent_creation_then_recovers_abandoned_network(app, saved, engine):
    with app.app_context():
        user = plugin.Users.query.get(saved.user_id)
        challenge = plugin.DockerChallenge.query.get(saved.challenge_id)
        record, token, _ = plugin.reserve_instance_creation(user, challenge, [], plugin.get_docker_config())
        labels = labels_for(str(user.id), challenge.id, token=token)
        network_id = plugin.create_instance_network(plugin.get_docker_config(), 'pending-network', labels)
        assert plugin.reconcile_docker_resources(plugin.get_docker_config())
        assert network_id in engine.networks
        record.updated_at -= plugin.INSTANCE_RECONCILE_GRACE + 1
        db.session.commit()
        assert plugin.reconcile_docker_resources(plugin.get_docker_config())
        assert engine.networks == {}
        assert plugin.DockerChallengeInstance.query.one().state == plugin.INSTANCE_FAILED


def test_orphan_reaper_leaves_unowned_and_ambiguous_networks(app, saved, engine):
    with app.app_context():
        docker = plugin.get_docker_config()
        owned = plugin.create_instance_network(docker, 'owned-orphan', labels_for())
        other_plugin = plugin.create_instance_network(docker, 'foreign', {'ctfd.plugin': 'other-plugin'})
        ambiguous = plugin.create_instance_network(docker, 'ambiguous', {'ctfd.plugin': plugin.PLUGIN_LABEL})
        stack_only = plugin.create_instance_network(docker, 'stack-only', {'ctfd.plugin': plugin.PLUGIN_LABEL, 'ctfd.stack.id': 'unknown-stack'})
        assert plugin.reconcile_docker_resources(docker)
        assert owned not in engine.networks
        assert set(engine.networks) == {other_plugin, ambiguous, stack_only}


def test_legacy_default_bridge_instance_stops_and_restarts_on_owned_network(app, saved, engine):
    client = client_for(app, saved.user_id)
    assert launch(client, saved.challenge_id).status_code == 200
    with app.app_context():
        tracker = plugin.DockerChallengeTracker.query.one()
        container = engine.containers[tracker.instance_id]
        del engine.networks[tracker.network_id]
        container['HostConfig'].pop('NetworkMode')
        container['NetworkSettings']['Networks'] = {'bridge': {'NetworkID': 'default-bridge'}}
        tracker.network_id = None
        tracker.instance_key = None
        db.session.commit()
    engine.networks['default-bridge'] = {'Id': 'default-bridge', 'Name': 'bridge', 'Driver': 'bridge', 'Labels': {}}
    engine.calls.clear()
    assert launch(client, saved.challenge_id, stopcontainer=True).status_code == 200
    assert not any('/networks/' in url for _, url, _ in engine.calls)
    assert 'default-bridge' in engine.networks
    assert launch(client, saved.challenge_id).status_code == 200
    with app.app_context():
        tracker = plugin.DockerChallengeTracker.query.one()
        assert tracker.network_id in engine.networks and tracker.network_id != 'default-bridge'
    assert 'default-bridge' in engine.networks


def test_legacy_compose_labels_and_network_name_still_clean_up(app, saved, engine):
    set_mode(app, saved, True)
    client = client_for(app, saved.user_id)
    assert launch(client, saved.challenge_id).status_code == 200
    with app.app_context():
        trackers = plugin.DockerChallengeTracker.query.all()
        network = engine.networks[trackers[0].network_id]
        network['Name'] = trackers[0].stack_id
        network['Labels'].pop('ctfd.instance.key')
        network['Labels'].pop('ctfd.operation.token')
        for tracker in trackers:
            tracker.instance_key = None
            container = engine.containers[tracker.instance_id]
            container['HostConfig']['NetworkMode'] = network['Name']
            container['Config']['Labels'].pop('ctfd.instance.key')
            container['Config']['Labels'].pop('ctfd.operation.token')
        db.session.commit()
    assert launch(client, saved.challenge_id, stopcontainer=True).status_code == 200
    assert engine.containers == engine.networks == {}


def test_orphan_network_with_unrelated_endpoint_requires_manual_cleanup(app, saved, engine):
    with app.app_context():
        docker = plugin.get_docker_config()
        network_id = plugin.create_instance_network(docker, 'owned-orphan', labels_for())
        engine.containers['unrelated'] = {
            'Id': 'unrelated', 'Name': 'unrelated', 'Config': {'Labels': {}},
            'HostConfig': {'NetworkMode': network_id}, 'State': {'Running': True},
            'NetworkSettings': {},
        }
        assert plugin.reconcile_docker_resources(docker)
    assert set(engine.containers) == {'unrelated'}
    assert network_id in engine.networks
