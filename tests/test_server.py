import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from hivemind.app import create_app
from hivemind.memory import Memory


class StubProvider:
    def __init__(self):
        self.cloud_started = threading.Event()
        self.cloud_release = threading.Event()
        self.inputs = []

    def status(self):
        return {"provider": "test", "available": True, "live": False}

    async def reply(self, messages, system):
        self.inputs.append((messages, system))
        if system.startswith("You plan"):
            return json.dumps({"capability": "cloud.answer", "node_id": None, "arguments": {}})
        if system.startswith("Complete this cloud task"):
            self.cloud_started.set()
            while not self.cloud_release.is_set():
                await asyncio.sleep(0.01)
            return "Background work finished."
        return json.dumps({"reply": "Conversation is still available.", "task": None})


@pytest.fixture
def server(tmp_path):
    provider = StubProvider()
    app = create_app(tmp_path, provider)
    with TestClient(app) as client:
        password = app.state.password_file.read_text().strip()
        assert client.post('/api/login', json={'password': password}).status_code == 200
        yield client, app, provider
    provider.cloud_release.set()


def pair(client, name='Desktop', capabilities=None):
    code = client.post('/api/pairing').json()['code']
    payload = {'code': code, 'name': name, 'kind': 'pc',
               'capabilities': capabilities or ['device.info', 'files.write']}
    response = client.post('/api/nodes/pair', json=payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def headers(device):
    return {'Authorization': 'Bearer ' + device['token']}


def wait_task(app, task_id, expected, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = app.state.store.task(task_id)
        if task['status'] == expected:
            return task
        time.sleep(0.01)
    pytest.fail(f"Task remained {task['status']}, expected {expected}")


def device_task(client, app, device, capability='device.info', arguments=None):
    response = client.post('/api/tasks', json={'prompt': 'Perform the specified test action',
                           'node_id': device['node_id'], 'capability': capability,
                           'arguments': arguments or {}})
    assert response.status_code == 200, response.text
    task = response.json()
    return wait_task(app, task['id'], 'awaiting_approval')


def test_owner_authentication_origin_and_logout(server):
    client, app, provider = server
    with TestClient(app) as stranger:
        assert stranger.get('/api/state').status_code == 401
        assert stranger.post('/api/tasks', json={'prompt': 'hello'}).status_code == 401
        assert stranger.post('/api/pairing').status_code == 401
        assert stranger.post('/api/login', json={'password': 'incorrect'}).status_code == 401
    assert client.post('/api/memories', json={'content': 'private'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/state').headers['cache-control'] == 'no-store'
    assert client.post('/api/logout').status_code == 200
    assert client.get('/api/state').status_code == 401


def test_pairing_is_one_time_and_expiring(server):
    client, app, provider = server
    device, payload = pair(client)
    assert client.post('/api/nodes/pair', json=payload).status_code == 400
    payload['code'] = client.post('/api/pairing').json()['code']
    app.state.store.execute('UPDATE pairing SET expires=?', (time.time()-1,))
    assert client.post('/api/nodes/pair', json=payload).status_code == 400
    state = client.get('/api/state').json()
    assert len(state['nodes']) == 1
    assert device['token'] not in json.dumps(state)
    assert 'token_hash' not in state['nodes'][0]


def test_conversation_can_see_completed_device_result(server):
    client, app, provider = server
    device, _ = pair(client)
    task = device_task(client, app, device)
    client.post(f"/api/tasks/{task['id']}/approve")
    claim = client.get('/api/node/work', headers=headers(device)).json()['task']
    result = {'system': 'test-system-marker'}
    assert client.post(f"/api/node/tasks/{task['id']}/result", headers=headers(device),
                       json={'lease_id': claim['lease_id'], 'status': 'completed', 'result': result}).status_code == 200
    assert client.post('/api/chat',json={'message':'What did the device report?','mode':'chat'}).status_code == 200
    assert 'test-system-marker' in provider.inputs[-1][1]
    assert 'untrusted data' in provider.inputs[-1][1]


def test_approval_binds_stored_action_and_only_assigned_node_can_finish(server):
    client, app, provider = server
    first, _ = pair(client, 'First')
    second, _ = pair(client, 'Second')
    original = {'path': 'notes.txt', 'content': 'Approved content'}
    task = device_task(client, app, first, 'files.write', original)
    assert client.get('/api/node/work', headers=headers(first)).json()['task'] is None
    assert client.post(f"/api/tasks/{task['id']}/approve", json={'arguments': {'path': 'other.txt', 'content': 'Unapproved'}}).status_code == 200
    assert client.post(f"/api/tasks/{task['id']}/approve").status_code == 409
    assert client.get('/api/node/work', headers=headers(second)).json()['task'] is None
    claimed = client.get('/api/node/work', headers=headers(first)).json()['task']
    assert claimed['arguments'] == original
    assert client.get('/api/node/work', headers=headers(first)).json()['task'] is None
    result = {'lease_id': claimed['lease_id'], 'status': 'completed', 'result': {'saved': True}}
    endpoint = f"/api/node/tasks/{task['id']}/result"
    assert client.post(endpoint, headers=headers(second), json=result).status_code == 409
    assert client.post(endpoint, headers=headers(first), json={**result, 'lease_id': 'wrong'}).status_code == 409
    assert client.post(endpoint, headers=headers(first), json=result).status_code == 200
    assert client.post(endpoint, headers=headers(first), json=result).status_code == 409
    assert app.state.store.task(task['id'])['status'] == 'completed'


def test_revocation_cancels_pending_work_and_rejects_old_token(server):
    client, app, provider = server
    device, _ = pair(client)
    task = device_task(client, app, device)
    assert client.delete('/api/nodes/' + device['node_id']).status_code == 200
    assert app.state.store.task(task['id'])['status'] == 'cancelled'
    assert client.get('/api/node/work', headers=headers(device)).status_code == 401
    assert client.post('/api/node/heartbeat', headers=headers(device)).status_code == 401
    assert client.post(f"/api/tasks/{task['id']}/approve").status_code == 409


def test_cancellation_waits_for_acknowledgement(server):
    client, app, provider = server
    device, _ = pair(client)
    queued = device_task(client, app, device)
    client.post(f"/api/tasks/{queued['id']}/approve")
    assert client.post(f"/api/tasks/{queued['id']}/cancel").json()['status'] == 'cancelled'
    assert client.get('/api/node/work', headers=headers(device)).json()['task'] is None
    running = device_task(client, app, device)
    client.post(f"/api/tasks/{running['id']}/approve")
    claimed = client.get('/api/node/work', headers=headers(device)).json()['task']
    assert client.post(f"/api/tasks/{running['id']}/cancel").json()['status'] == 'cancel_requested'
    heartbeat = client.post('/api/node/heartbeat', headers=headers(device)).json()
    assert running['id'] in heartbeat['cancelled_task_ids']
    assert client.post(f"/api/node/tasks/{running['id']}/result", headers=headers(device),
                       json={'lease_id': claimed['lease_id'], 'status': 'cancelled', 'result': 'stopped'}).status_code == 200
    assert app.state.store.task(running['id'])['status']=='cancelled'
    assert client.post(f"/api/node/tasks/{running['id']}/result", headers=headers(device),
                       json={'lease_id': claimed['lease_id'], 'status': 'completed', 'result': 'late'}).status_code == 409


def test_restart_does_not_replay_running_action(tmp_path):
    app = create_app(tmp_path, StubProvider())
    with TestClient(app) as client:
        client.post('/api/login', json={'password': app.state.password_file.read_text().strip()})
        device, _ = pair(client)
        task = device_task(client, app, device)
        client.post(f"/api/tasks/{task['id']}/approve")
        claimed = client.get('/api/node/work', headers=headers(device)).json()['task']
    restored = create_app(tmp_path, StubProvider())
    with TestClient(restored) as client:
        assert restored.state.store.task(task['id'])['status'] == 'outcome_unknown'
        assert client.get('/api/node/work', headers=headers(device)).json()['task'] is None
        assert client.post(f"/api/node/tasks/{task['id']}/result", headers=headers(device),
                           json={'lease_id': claimed['lease_id'], 'status': 'completed', 'result': 'late'}).status_code == 409


def test_memory_delete_removes_retrieval(server):
    client, app, provider = server
    memory = client.post('/api/memories', json={'content': 'Favorite tree: sequoia'}).json()
    assert Memory(app.state.store).retrieve('sequoia')[0]['id'] == memory['id']
    assert client.delete('/api/memories/' + memory['id']).status_code == 200
    assert Memory(app.state.store).retrieve('sequoia') == []
    assert client.get('/api/memories').json() == []


def test_chat_remains_available_during_background_task(server):
    client, app, provider = server
    task = client.post('/api/tasks', json={'prompt': 'Slow background answer', 'capability': 'cloud.answer'}).json()
    assert provider.cloud_started.wait(2)
    assert app.state.store.task(task['id'])['status'] == 'running'
    with ThreadPoolExecutor(max_workers=1) as pool:
        reply = pool.submit(client.post, '/api/chat', json={'message': 'Hello while you work', 'mode': 'chat'}).result(timeout=2)
    assert reply.status_code == 200
    assert reply.json()['message']['content'] == 'Conversation is still available.'
    assert app.state.store.task(task['id'])['status'] == 'running'
    provider.cloud_release.set()
    wait_task(app, task['id'], 'completed')


def test_cancelling_cloud_work_does_not_wait_for_a_device_ack(server):
    client, app, provider = server
    device, _ = pair(client)
    task = client.post('/api/tasks', json={'prompt': 'Slow reasoning', 'node_id': device['node_id'], 'capability': 'cloud.answer'}).json()
    assert provider.cloud_started.wait(2)
    assert client.post('/api/tasks/'+task['id']+'/cancel').json()['status']=='cancelled'
    provider.cloud_release.set()
    assert app.state.store.task(task['id'])['status']=='cancelled'


def test_full_latest_message_reaches_front_agent(server):
    client, app, provider = server
    message = 'x' * 7000 + ' Keep this a discussion; do not create any task.'
    assert client.post('/api/chat', json={'message': message, 'mode': 'auto'}).status_code == 200
    assert provider.inputs[-1][0][-1]['content'] == message


def test_expired_lease_cannot_report_success(server):
    client, app, provider = server
    device, _ = pair(client)
    task = device_task(client, app, device)
    client.post(f"/api/tasks/{task['id']}/approve")
    claimed = client.get('/api/node/work', headers=headers(device)).json()['task']
    app.state.store.execute('UPDATE tasks SET lease_until=? WHERE id=?', (time.time()-1, task['id']))
    assert client.post(f"/api/node/tasks/{task['id']}/result", headers=headers(device),
                       json={'lease_id': claimed['lease_id'], 'status': 'completed', 'result': 'too late'}).status_code == 409


def test_revocation_during_running_task_is_not_success(server):
    client, app, provider = server
    device, _ = pair(client)
    task = device_task(client, app, device)
    client.post(f"/api/tasks/{task['id']}/approve")
    claimed = client.get('/api/node/work', headers=headers(device)).json()['task']
    client.delete('/api/nodes/' + device['node_id'])
    assert app.state.store.task(task['id'])['status'] == 'outcome_unknown'
    assert client.post(f"/api/node/tasks/{task['id']}/result", headers=headers(device),
                       json={'lease_id': claimed['lease_id'], 'status': 'completed', 'result': 'late'}).status_code == 401


def test_malformed_json_objects_and_lengths_are_rejected(server):
    client, app, provider = server
    for endpoint in ('/api/login', '/api/memories', '/api/providers'):
        assert client.post(endpoint, json=[]).status_code == 400
        assert client.post(endpoint, content='bad-json', headers={'Content-Type': 'application/json'}).status_code == 400
    assert client.post('/api/login', content='{}', headers={'Content-Length': 'oops'}).status_code == 400


def test_provider_errors_do_not_expose_credentials(server):
    client, app, provider = server

    async def failing_reply(messages, system):
        raise RuntimeError('Upstream failed with secret-test-credential')

    provider.reply = failing_reply
    response = client.post('/api/chat', json={'message': 'Hi', 'mode': 'chat'})
    assert response.status_code == 502
    assert 'secret-test-credential' not in response.text
    task = client.post('/api/tasks', json={'prompt': 'Model fails here'}).json()
    wait_task(app, task['id'], 'failed')
    assert 'secret-test-credential' not in json.dumps(app.state.store.task(task['id']))


def test_provider_settings_encrypt_key_and_reject_malformed_url(server):
    client, app, provider = server
    settings = {'type': 'compatible', 'base_url': 'https://example.com/v1',
                'model': 'test-model', 'api_key': 'secret-test-credential'}
    response = client.post('/api/providers', json=settings)
    assert response.status_code == 200
    assert settings['api_key'] not in response.text
    assert settings['api_key'] not in client.get('/api/providers').text
    encrypted = app.state.password_file.parent / 'provider.enc'
    assert settings['api_key'] not in encrypted.read_text()
    for bad in ({'base_url': 12}, {'base_url': 'https://example.com/v1?secret=x'}, {'model': []}):
        assert client.post('/api/providers', json={**settings, **bad}).status_code == 400
