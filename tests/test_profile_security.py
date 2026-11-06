import asyncio
import sys

import pytest

from hivemind.account_login import AccountLogin, AccountLoginError
from hivemind.coordinator import Coordinator
from hivemind.store import Store
from test_profiles import host, pair, register, signin_cookie, task


def test_encoded_and_redirected_paths_preserve_profile_identity(host):
    app, client, _, owner_cookie = host
    client.post('/api/memories', json={'content': 'OWNER_PRIVATE_SENTINEL'})
    member, member_cookie = register(client, owner_cookie, 'alice')
    client.post('/api/memories', json={'content': 'member note'})
    for path in ('/api/state/', '/%61pi/state', '/api%2fstate', '/api/%73tate',
                 '/%61pi%2fstate', '/api/../api/state', '/api/%2e%2e/api/state', '/%2fapi/state'):
        response = client.get(path)
        assert 'OWNER_PRIVATE_SENTINEL' not in response.text
        assert response.status_code in (200, 404)
        if response.status_code == 200:
            assert response.json()['user']['id'] == member['id']
    code = client.post('/api/pairing').json()['code']
    body={'code':code,'name':'Encoded endpoint','kind':'pc','capabilities':['device.info']}
    assert client.post('/api/nodes/%70air/', json=body).status_code in (404,405)
    paired = client.post('/api/nodes/%70air', json=body)
    assert paired.status_code == 200
    assert paired.json()['token'].startswith(member['id']+'.')
    signin_cookie(client, owner_cookie)
    assert client.get('/api/state').json()['nodes'] == []


def test_cookie_and_device_credentials_route_separately(host):
    app, client, _, owner_cookie = host
    member, member_cookie = register(client, owner_cookie, 'alice')
    device = pair(client)
    task_id = task(app, client, member['id'], device)
    assert client.post('/api/tasks/'+task_id+'/approve').status_code == 200
    signin_cookie(client, owner_cookie)
    assert client.get('/api/state', headers={'Authorization':'Bearer '+device['token']}).json()['user']['id'] == 'owner'
    assert client.get('/api/%6eode/work/', headers={'Authorization':'Bearer '+device['token']}).status_code == 404
    claim = client.get('/api/%6eode/work', headers={'Authorization':'Bearer '+device['token']})
    assert claim.status_code == 200 and claim.json()['task']['id'] == task_id
    stripped = device['token'].split('.',1)[1]
    assert client.get('/api/node/work', headers={'Authorization':'Bearer '+stripped}).status_code == 401
    signin_cookie(client, member_cookie)
    assert client.get('/api/node/work').status_code == 401
    client.cookies.clear()
    assert client.get('/api/state', headers={'Authorization':'Bearer '+device['token']}).status_code == 401


def test_closed_coordinator_cannot_create_or_schedule_tasks(tmp_path):
    store = Store(tmp_path/'test.db')
    async def run():
        coordinator = Coordinator(store, None, None, lambda: [])
        await coordinator.close()
        with pytest.raises(ValueError, match='closed'):
            coordinator.create('Must not create work')
        coordinator.schedule('does-not-exist')
        assert coordinator.jobs == {}
        assert store.rows('SELECT * FROM tasks') == []
    asyncio.run(run())


def test_closing_account_login_rejects_start_already_waiting_on_lock(tmp_path, monkeypatch):
    runtime = tmp_path/'runtime'
    (runtime/'hermes_cli').mkdir(parents=True)
    (runtime/'hermes_cli'/'web_server.py').write_text('')
    state = tmp_path/'account'
    state.mkdir()
    manager = AccountLogin(state, hermes_root=str(runtime), python=sys.executable)
    async def never_spawn(*args, **kwargs):
        pytest.fail('A disabled account must not launch a new sign-in worker')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', never_spawn)
    async def run():
        await manager._lock.acquire()
        pending = asyncio.create_task(manager.start('test-model'))
        await asyncio.sleep(0)
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        manager._lock.release()
        with pytest.raises(AccountLoginError, match='closed'):
            await pending
        await closing
        assert manager._process is None
    asyncio.run(run())
