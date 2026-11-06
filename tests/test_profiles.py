import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from hivemind.app import create_app
from hivemind.providers import MockProvider, isolated_environment
from hivemind.profiles import reset_owner_password, verify_password


PASSWORD = 'a long example password'


@pytest.fixture
def host(tmp_path):
    app = create_app(tmp_path, MockProvider())
    with TestClient(app) as client:
        password = app.state.password_file.read_text()
        assert client.post('/api/login', json={'password': password}).status_code == 200
        owner_cookie = client.cookies.get('hive_session')
        yield app, client, password, owner_cookie


def signin_cookie(client, cookie):
    client.cookies.clear()
    client.cookies.set('hive_session', cookie, domain='testserver.local', path='/')


def register(client, owner_cookie, username):
    signin_cookie(client, owner_cookie)
    invitation = client.post('/api/invitations').json()
    response = client.post('/api/register', json={'invite': invitation['code'], 'username': username,
                           'name': username.title(), 'password': PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()['user'], client.cookies.get('hive_session')


def pair(client):
    code = client.post('/api/pairing').json()['code']
    response = client.post('/api/nodes/pair', json={'code': code, 'name': 'Test device', 'kind': 'pc',
                                                 'capabilities': ['device.info']})
    assert response.status_code == 200, response.text
    return response.json()


def task(app, client, identity, node):
    response = client.post('/api/tasks', json={'prompt': 'Read scope', 'capability': 'device.info',
                           'node_id': node['node_id']})
    assert response.status_code == 200
    task_id = response.json()['id']
    deadline = time.monotonic() + 2
    store = app.state.runtimes[identity].state.store
    while time.monotonic() < deadline:
        if store.task(task_id)['status'] == 'awaiting_approval':
            return task_id
        time.sleep(.01)
    pytest.fail('Task did not become reviewable')


def test_profiles_isolate_data_devices_tasks_and_credentials(host):
    app, client, _, owner_cookie = host
    client.post('/api/memories', json={'content': 'owner private note'})
    owner_device = pair(client)
    owner_task = task(app, client, 'owner', owner_device)
    first, first_cookie = register(client, owner_cookie, 'Alice')
    assert first['username'] == 'alice'
    state = client.get('/api/state').json()
    assert state['user']['id'] == first['id']
    assert state['nodes'] == state['tasks'] == state['messages'] == state['memories'] == []
    assert state['provider']['provider'] == 'unconfigured'
    assert state['provider']['available'] is False
    assert client.post('/api/providers', json={'type': 'hermes'}).status_code == 403
    for url in ('http://localhost:8080/v1', 'https://127.0.0.1/v1', 'https://api.openai.com.evil.example/v1'):
        assert client.post('/api/providers', json={'type': 'compatible', 'base_url': url, 'model': 'test'}).status_code == 403
    assert client.get(f'/api/tasks/{owner_task}/events').json() == []
    assert client.post(f'/api/tasks/{owner_task}/approve').status_code == 409
    assert client.post(f'/api/tasks/{owner_task}/cancel').status_code == 404
    assert client.post(f'/api/nodes/{owner_device["node_id"]}/permissions', json={'capabilities': []}).status_code == 400
    assert client.post('/api/tasks', json={'prompt': 'Use foreign device', 'node_id': owner_device['node_id']}).status_code == 400
    client.delete(f'/api/nodes/{owner_device["node_id"]}')
    first_device = pair(client)
    first_task = task(app, client, first['id'], first_device)
    client.post('/api/memories', json={'content': 'alice private note'})
    second, second_cookie = register(client, owner_cookie, 'bob')
    assert client.get('/api/state').json()['memories'] == []
    assert client.get(f'/api/tasks/{first_task}/events').json() == []
    assert client.post(f'/api/tasks/{first_task}/approve').status_code == 409
    assert client.get('/api/invitations').status_code == 403
    assert client.post('/api/invitations').status_code == 403
    assert client.get('/api/profiles').status_code == 403
    # Replacing a routing prefix does not turn the member's credential into another identity.
    forged = second['id'] + '.' + first_cookie.split('.', 1)[1]
    signin_cookie(client, forged)
    assert client.get('/api/state').status_code == 401
    signin_cookie(client, first_cookie)
    assert client.post(f'/api/tasks/{first_task}/approve').status_code == 200
    headers = {'Authorization': 'Bearer ' + first_device['token']}
    claim = client.get('/api/node/work', headers=headers).json()['task']
    assert claim['id'] == first_task
    assert client.post(f'/api/node/tasks/{owner_task}/result', headers=headers,
                       json={'lease_id': claim['lease_id'], 'status': 'completed'}).status_code == 409
    assert client.get('/api/state').json()['memories'][0]['content'] == 'alice private note'
    signin_cookie(client, owner_cookie)
    state = client.get('/api/state').json()
    assert [n['id'] for n in state['nodes']] == [owner_device['node_id']]
    assert state['memories'][0]['content'] == 'owner private note'
    assert app.state.store.task(owner_task)['status'] == 'awaiting_approval'
    assert app.state.runtimes[first['id']].state.store.path != app.state.store.path


def test_invitation_one_use_expiry_casefold_and_origin(host):
    app, client, _, owner_cookie = host
    invitation = client.post('/api/invitations').json()
    data = {'invite': invitation['code'], 'username': 'Alice', 'name': 'Alice', 'password': PASSWORD}
    assert client.post('/api/register', json=data, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/register', json=dict(data, password='short')).status_code == 400
    assert client.post('/api/register', json=data).status_code == 200
    assert client.post('/api/register', json=dict(data, username='bob')).status_code == 400
    signin_cookie(client, owner_cookie)
    second = client.post('/api/invitations').json()
    assert client.post('/api/register', json=dict(data, invite=second['code'], username='ALICE')).status_code == 409
    assert client.post('/api/register', json=dict(data, invite=second['code'], username='bob')).status_code == 200
    signin_cookie(client, owner_cookie)
    old = client.post('/api/invitations').json()
    with app.state.profiles.connect() as db:
        db.execute('UPDATE invitations SET expires_at=? WHERE id=?', (time.time()-1, old['id']))
    # A new IP has a fresh rate bucket; expiry itself must also reject registration.
    with TestClient(app, client=('another-client', 50000)) as other:
        assert other.post('/api/register', json=dict(data, invite=old['code'], username='charlie')).status_code == 400
    listed = client.get('/api/invitations').json()
    assert all('code' not in row and 'code_hash' not in row for row in listed)


def test_password_change_revokes_previous_sessions_and_preserves_owner(host):
    app, client, _, owner_cookie = host
    member, member_cookie = register(client, owner_cookie, 'alice')
    response = client.patch('/api/profile', json={'name': 'New name', 'password': 'a new long secret password', 'current_password': 'wrong'})
    assert response.status_code == 400
    assert client.get('/api/profile').status_code == 200
    response = client.patch('/api/profile', json={'name': 'New name', 'password': 'a new long secret password', 'current_password': PASSWORD})
    assert response.status_code == 200
    new_cookie = client.cookies.get('hive_session')
    assert new_cookie != member_cookie
    assert client.get('/api/state').json()['user']['name'] == 'New name'
    signin_cookie(client, member_cookie)
    assert client.get('/api/state').status_code == 401
    assert client.post('/api/login', json={'username': 'alice', 'password': PASSWORD}).status_code == 401
    assert client.post('/api/login', json={'username': 'ALICE', 'password': 'a new long secret password'}).status_code == 200
    stored = app.state.profiles.get(member['id'])
    assert stored['password_hash'].startswith('scrypt$')
    assert 'secret password' not in stored['password_hash']
    signin_cookie(client, owner_cookie)
    assert client.get('/api/state').status_code == 200


def test_admin_disables_member_and_revokes_device(host):
    app, client, _, owner_cookie = host
    member, member_cookie = register(client, owner_cookie, 'alice')
    device = pair(client)
    task_id = task(app, client, member['id'], device)
    signin_cookie(client, owner_cookie)
    assert client.post(f'/api/profiles/{member["id"]}/disable').status_code == 200
    assert client.post('/api/profiles/owner/disable').status_code == 400
    signin_cookie(client, member_cookie)
    assert client.get('/api/state').status_code == 401
    assert client.get('/api/node/work', headers={'Authorization': 'Bearer ' + device['token']}).status_code == 401
    assert client.post('/api/login', json={'username': 'alice', 'password': PASSWORD}).status_code == 401
    assert app.state.runtimes[member['id']].state.store.task(task_id)['status'] == 'cancelled'


def test_owner_password_change_restart_and_recovery(host, tmp_path):
    app, client, password, owner_cookie = host
    assert client.patch('/api/profile', json={'password': PASSWORD, 'current_password': password}).status_code == 200
    assert not app.state.password_file.exists()
    restarted = create_app(tmp_path, MockProvider())
    assert not restarted.state.password_file.exists()
    with TestClient(restarted) as new:
        assert new.post('/api/login', json={'password': password}).status_code == 401
        assert new.post('/api/login', json={'password': PASSWORD}).status_code == 200
        reset_owner_password(tmp_path)
        assert new.get('/api/state').status_code == 401
        assert new.post('/api/login', json={'password': restarted.state.password_file.read_text()}).status_code == 200


def test_member_provider_key_never_inherits_host_environment(host, monkeypatch):
    app, client, _, owner_cookie = host
    monkeypatch.setenv('HIVEMIND_API_KEY', 'host-secret-test-value')
    member, _ = register(client, owner_cookie, 'alice')
    response = client.post('/api/providers', json={'type': 'compatible', 'base_url': 'https://api.openai.com/v1', 'model': 'test-model'})
    assert response.status_code == 200
    assert response.json()['authentication'] == 'No API key supplied'
    profile_dir = app.state.profiles.state / 'profiles' / member['id']
    from cryptography.fernet import Fernet
    saved = json.loads(Fernet((profile_dir/'settings.key').read_bytes()).decrypt((profile_dir/'provider.enc').read_bytes()))
    assert saved['api_key'] == ''
    assert 'host-secret-test-value' not in json.dumps(saved)


def test_isolated_subscription_environment_has_no_host_credentials(monkeypatch, tmp_path):
    for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'HIVEMIND_API_KEY', 'HERMES_PROFILE', 'HERMES_CODEX_BASE_URL', 'HTTP_PROXY'):
        monkeypatch.setenv(key, 'host-secret')
    env = isolated_environment(tmp_path)
    assert 'host-secret' not in env.values()
    assert env['HERMES_HOME'] == str(tmp_path)
    assert env['CODEX_HOME'] == str(tmp_path/'codex')


def test_disabling_profile_blocks_an_inflight_reply_from_creating_work(host, monkeypatch):
    import hivemind.app as app_module
    started, release = threading.Event(), threading.Event()
    class DelayedProvider:
        def status(self):
            return {'available':True,'provider':'test'}
        async def reply(self, messages, system):
            started.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            return json.dumps({'reply':'Starting work', 'task':'Do some background work'})
    monkeypatch.setattr(app_module, 'Provider', lambda config: DelayedProvider())
    app, client, _, owner_cookie = host
    member, member_cookie = register(client, owner_cookie, 'alice')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, '/api/chat', json={'message':'Do some work'},
                             headers={'Cookie':'hive_session='+member_cookie})
        assert started.wait(3)
        signin_cookie(client, owner_cookie)
        assert client.post(f'/api/profiles/{member["id"]}/disable').status_code == 200
        release.set()
        assert future.result(timeout=3).status_code == 502
    store = app.state.runtimes[member['id']].state.store
    assert store.rows('SELECT * FROM tasks') == []
    assert store.rows("SELECT * FROM messages WHERE role='assistant'") == []
