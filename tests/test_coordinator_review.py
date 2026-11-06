import json
import time

from fastapi.testclient import TestClient

from hivemind.app import create_app
from hivemind.store import uid
from test_workflows import pair, wait_until, child


class ReviewPlanner:
    async def reply(self, messages, system):
        data = json.loads(messages[-1]['content'])
        if data['completed_steps']:
            return json.dumps({'done': 'The verified steps are complete.'})
        return json.dumps({'capability': 'device.info', 'node_id': data['devices'][0]['id'], 'arguments': {}})

    def status(self):
        return {'provider': 'test'}


def seed_completed_workflow(store, children=1):
    parent = uid()
    now = time.time()
    store.execute("INSERT INTO tasks(id,prompt,status,arguments,created_at,updated_at,is_workflow) VALUES (?,?,?,?,?,?,1)",
                  (parent, 'Review completed work', 'waiting_children', '{}', now, now))
    for step in range(children):
        store.execute("INSERT INTO tasks(id,prompt,status,arguments,result,created_at,updated_at,parent_id,step_no,is_workflow) VALUES (?,?,?,?,?,?,?,?,?,0)",
                      (uid(), 'Completed action', 'completed', '{}', '{"ok":true}', now, now, parent, step+1))
    return parent


def test_restart_resumes_parent_with_completed_child(tmp_path):
    app = create_app(tmp_path, ReviewPlanner())
    parent = seed_completed_workflow(app.state.store)
    with TestClient(app):
        wait_until(lambda: app.state.store.task(parent)['status'] == 'completed')


def test_cancel_child_propagates_to_parent(tmp_path):
    app = create_app(tmp_path, ReviewPlanner())
    with TestClient(app) as client:
        client.post('/api/login', json={'password': app.state.password_file.read_text()})
        device, auth = pair(client)
        parent = client.post('/api/tasks', json={'prompt': 'Read device info', 'node_id': device['node_id']}).json()
        action = wait_until(lambda: child(app, parent['id']))
        assert client.post('/api/tasks/' + action['id'] + '/cancel').json()['status'] == 'cancelled'
        wait_until(lambda: app.state.store.task(parent['id'])['status'] == 'cancelled')


def test_manifest_change_invalidates_previously_approved_harness_action(tmp_path):
    app = create_app(tmp_path, ReviewPlanner())
    with TestClient(app) as client:
        client.post('/api/login', json={'password': app.state.password_file.read_text()})
        caps = ['device.info', 'harness.run']
        old_manifest = [{'id': 'agent', 'name': 'Old agent'}]
        device, auth = pair(client, caps, old_manifest)
        client.post('/api/nodes/' + device['node_id'] + '/permissions', json={'capabilities': caps})
        task = client.post('/api/tasks', json={'prompt': 'Use registered agent', 'node_id': device['node_id'],
                           'capability': 'harness.run', 'arguments': {'harness_id': 'agent', 'goal': 'Read project status', 'timeout': 30}}).json()
        wait_until(lambda: app.state.store.task(task['id'])['status'] == 'awaiting_approval')
        client.post('/api/tasks/' + task['id'] + '/approve')
        client.post('/api/node/heartbeat', headers=auth, json={'capabilities': caps, 'harnesses': [{'id': 'agent', 'name': 'Changed agent'}]})
        client.post('/api/nodes/' + device['node_id'] + '/permissions', json={'capabilities': caps})
        assert client.get('/api/node/work', headers=auth).json()['task'] is None
        assert app.state.store.task(task['id'])['status'] in {'failed', 'cancelled'}


def test_eight_completed_steps_can_still_finish(tmp_path):
    app = create_app(tmp_path, ReviewPlanner())
    parent = seed_completed_workflow(app.state.store, children=8)
    with TestClient(app):
        wait_until(lambda: app.state.store.task(parent)['status'] == 'completed')
