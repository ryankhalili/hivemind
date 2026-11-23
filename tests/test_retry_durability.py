import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hivemind.app import create_app
from test_workflows import pair

INCOMPLETE_ACTION = 'The saved action is incomplete. Start a new task after reviewing the original request.'
READ_ARGUMENTS = {'path': 'notes/report.txt'}


class CountingProvider:
    def __init__(self):
        self.calls = []

    async def reply(self, messages, system):
        self.calls.append((messages, system))
        return json.dumps({'done': 'The synthetic workflow is complete.'})

    def status(self):
        return {'provider': 'test'}


@pytest.fixture
def controlled_factory(tmp_path, monkeypatch):
    @contextmanager
    def open_host():
        provider = CountingProvider()
        app = create_app(tmp_path / 'state', provider)
        coordinator = app.state.runtimes['owner'].state.coordinator
        scheduled = []
        monkeypatch.setattr(coordinator, 'schedule', lambda *args: scheduled.append(args))
        with TestClient(app) as client:
            response = client.post('/api/login', json={'password': app.state.password_file.read_text()})
            assert response.status_code == 200, response.text
            yield SimpleNamespace(app=app, client=client, store=app.state.store,
                                  coordinator=coordinator, scheduled=scheduled, provider=provider)
    return open_host


@pytest.fixture
def controlled(controlled_factory):
    with controlled_factory() as host:
        yield host


def run_next(host):
    arguments = host.scheduled.pop(0)
    host.client.portal.call(host.coordinator.run, *arguments)
    return host.store.task(arguments[0])


def create_read(host, device):
    response = host.client.post('/api/tasks', json={
        'prompt': 'Read the requested file', 'node_id': device['node_id'],
        'capability': 'files.read', 'arguments': READ_ARGUMENTS,
    })
    assert response.status_code == 200, response.text
    return response.json()


def assert_fresh_read_retry(host, previous, retried):
    assert retried['id'] != previous['id']
    assert retried['is_workflow'] == 0
    assert retried['capability'] == 'files.read'
    assert retried['node_id'] == previous['node_id']
    assert retried['arguments'] == READ_ARGUMENTS
    assert retried['approval'] == 'pending'
    assert retried['approval_scope_id'] == ''
    assert retried['lease_id'] is None
    assert retried['lease_until'] is None
    assert retried['result'] is None
    assert retried['error'] is None
    assert host.provider.calls == []
    events = host.store.rows("SELECT detail FROM events WHERE task_id=? AND kind='retry_of'", (retried['id'],))
    assert events == [{'detail': previous['id']}]


def test_retry_after_permission_failure_preserves_concrete_action(controlled):
    host = controlled
    device, auth = pair(host.client, ['files.read'])
    permissions = '/api/nodes/' + device['node_id'] + '/permissions'
    assert host.client.post(permissions, json={'capabilities': []}).status_code == 200
    original = create_read(host, device)
    failed = run_next(host)
    assert failed['status'] == 'failed'
    assert failed['capability'] == 'files.read'
    assert failed['node_id'] == device['node_id']
    assert failed['arguments'] == READ_ARGUMENTS
    assert host.client.post(permissions, json={'capabilities': ['files.read']}).status_code == 200

    response = host.client.post('/api/tasks/' + original['id'] + '/retry', json={})
    assert response.status_code == 200, response.text
    retried = response.json()
    assert_fresh_read_retry(host, failed, retried)
    assert run_next(host)['status'] == 'awaiting_approval'
    assert host.client.get('/api/node/work', headers=auth).json()['task'] is None
    assert host.provider.calls == []


def test_restart_before_preparation_preserves_action_without_dispatch(controlled_factory):
    with controlled_factory() as host:
        device, auth = pair(host.client, ['files.read'])
        original = create_read(host, device)
        assert host.store.task(original['id'])['status'] == 'planning'
        assert len(host.scheduled) == 1
        assert host.provider.calls == []

    with controlled_factory() as restored:
        failed = restored.store.task(original['id'])
        assert failed['status'] == 'failed'
        assert restored.scheduled == []
        assert restored.provider.calls == []
        assert restored.client.get('/api/node/work', headers=auth).json()['task'] is None

        response = restored.client.post('/api/tasks/' + original['id'] + '/retry', json={})
        assert response.status_code == 200, response.text
        assert_fresh_read_retry(restored, failed, response.json())
        assert run_next(restored)['status'] == 'awaiting_approval'
        assert restored.client.get('/api/node/work', headers=auth).json()['task'] is None
        assert restored.provider.calls == []


def test_prepared_failed_action_retry_requires_fresh_approval(controlled):
    host = controlled
    device, auth = pair(host.client, ['files.read'])
    original = create_read(host, device)
    assert run_next(host)['status'] == 'awaiting_approval'
    assert host.client.post('/api/tasks/' + original['id'] + '/approve', json={}).status_code == 200
    claim = host.client.get('/api/node/work', headers=auth).json()['task']
    assert claim['id'] == original['id']
    response = host.client.post('/api/node/tasks/' + original['id'] + '/result', headers=auth,
                                json={'lease_id': claim['lease_id'], 'status': 'failed',
                                      'result': {'partial': 'synthetic evidence'}, 'error': 'Synthetic read failure'})
    assert response.status_code == 200, response.text
    failed = host.store.task(original['id'])
    assert failed['approval'] == 'explicit'
    assert failed['lease_id'] == claim['lease_id']

    response = host.client.post('/api/tasks/' + original['id'] + '/retry', json={})
    assert response.status_code == 200, response.text
    assert_fresh_read_retry(host, failed, response.json())
    assert run_next(host)['status'] == 'awaiting_approval'
    assert host.client.get('/api/node/work', headers=auth).json()['task'] is None
    assert host.store.task(original['id']) == failed


@pytest.mark.parametrize('capability', [None, ''])
@pytest.mark.parametrize('status', ['failed', 'cancelled', 'outcome_unknown'])
def test_legacy_concrete_action_without_capability_cannot_be_replanned(controlled, capability, status):
    host = controlled
    device, auth = pair(host.client, ['files.read'])
    original = create_read(host, device)
    host.scheduled.clear()
    host.store.execute('UPDATE tasks SET capability=? WHERE id=?', (capability, original['id']))
    host.store.update_task(original['id'], status)
    previous_tasks = host.store.rows('SELECT * FROM tasks ORDER BY id')
    previous_events = host.store.rows('SELECT * FROM events ORDER BY id')

    response = host.client.post('/api/tasks/' + original['id'] + '/retry',
                                json={'confirmed_outcome': True} if status == 'outcome_unknown' else {})
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == INCOMPLETE_ACTION
    assert host.store.rows('SELECT * FROM tasks ORDER BY id') == previous_tasks
    assert host.store.rows('SELECT * FROM events ORDER BY id') == previous_events
    assert host.scheduled == []
    assert host.provider.calls == []
    assert host.client.get('/api/node/work', headers=auth).json()['task'] is None


def test_true_workflow_retry_still_calls_planner(controlled):
    host = controlled
    response = host.client.post('/api/tasks', json={'prompt': 'Complete a synthetic workflow'})
    assert response.status_code == 200, response.text
    original = response.json()
    host.scheduled.clear()
    host.store.update_task(original['id'], 'failed')

    response = host.client.post('/api/tasks/' + original['id'] + '/retry', json={})
    assert response.status_code == 200, response.text
    retried = response.json()
    assert retried['id'] != original['id']
    assert retried['is_workflow'] == 1
    assert retried['capability'] is None
    assert run_next(host)['status'] == 'completed'
    assert len(host.provider.calls) == 1
    assert host.provider.calls[0][1].startswith('You plan')


def test_legacy_action_retry_keeps_child_and_unknown_guards_first(controlled):
    host = controlled
    device, _ = pair(host.client, ['files.read'])
    original = create_read(host, device)
    host.scheduled.clear()
    host.store.execute('UPDATE tasks SET capability=NULL WHERE id=?', (original['id'],))
    host.store.update_task(original['id'], 'outcome_unknown')
    response = host.client.post('/api/tasks/' + original['id'] + '/retry', json={})
    assert response.status_code == 409
    assert response.json()['detail'] == 'Check the device first, then confirm the previous outcome was reviewed.'

    parent = host.client.post('/api/tasks', json={'prompt': 'Synthetic parent'}).json()
    host.scheduled.clear()
    host.store.execute('UPDATE tasks SET parent_id=? WHERE id=?', (parent['id'], original['id']))
    host.store.update_task(original['id'], 'failed')
    previous_tasks = host.store.rows('SELECT * FROM tasks ORDER BY id')
    previous_events = host.store.rows('SELECT * FROM events ORDER BY id')
    response = host.client.post('/api/tasks/' + original['id'] + '/retry', json={})
    assert response.status_code == 409
    assert response.json()['detail'] == 'Retry the parent task so earlier results remain understandable.'
    assert host.store.rows('SELECT * FROM tasks ORDER BY id') == previous_tasks
    assert host.store.rows('SELECT * FROM events ORDER BY id') == previous_events
    assert host.scheduled == []
    assert host.provider.calls == []
