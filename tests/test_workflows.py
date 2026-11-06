import json
import time
from fastapi.testclient import TestClient
from hivemind.app import create_app


class Planner:
    async def reply(self,messages,system):
        if system.startswith('You plan'):
            data=json.loads(messages[-1]['content']); steps=data['completed_steps']; device=data['devices'][0]['id']
            if len(steps)==0:
                return json.dumps({'capability':'device.info','node_id':device,'arguments':{}})
            if len(steps)==1:
                return json.dumps({'capability':'files.write','node_id':device,'arguments':{'path':'hello.txt','content':'Hello World'}})
            return json.dumps({'done':'Created hello.txt with Hello World.'})
        return json.dumps({'reply':'Hello','task':None})

    def status(self):
        return {'provider':'test'}


def setup(tmp_path):
    app=create_app(tmp_path,Planner())
    client=TestClient(app)
    return app,client


def pair(client,capabilities=None,harnesses=None):
    code=client.post('/api/pairing',json={}).json()['code']
    d=client.post('/api/nodes/pair',json={'code':code,'name':'Test PC','kind':'pc',
                 'capabilities':capabilities or ['device.info','files.write'],'harnesses':harnesses or []}).json()
    return d,{'Authorization':'Bearer '+d['token']}


def wait_until(fn,timeout=3):
    deadline=time.time()+timeout
    while time.time()<deadline:
        value=fn()
        if value:return value
        time.sleep(.01)
    raise AssertionError('State did not arrive')


def child(app,parent):
    rows=app.state.store.rows("SELECT id FROM tasks WHERE parent_id=? AND status='awaiting_approval' ORDER BY step_no DESC",(parent,))
    return app.state.store.task(rows[0]['id']) if rows else None


def test_two_step_request_and_shared_parent_id(tmp_path):
    app,c=setup(tmp_path)
    with c:
        c.post('/api/login',json={'password':app.state.password_file.read_text()})
        d,auth=pair(c)
        parent=c.post('/api/tasks',json={'prompt':'Check the folder and create Hello World','node_id':d['node_id']}).json()
        for step,result in [(1,{'shared_folder':'sandbox'}),(2,{'created':True,'path':'hello.txt'})]:
            action=wait_until(lambda:child(app,parent['id']))
            assert action['step_no']==step
            assert c.get('/api/node/work',headers=auth).json()['task'] is None
            c.post('/api/tasks/'+action['id']+'/approve')
            claim=c.get('/api/node/work',headers=auth).json()['task']
            assert claim['parent_id']==parent['id']
            assert c.post('/api/node/tasks/'+claim['id']+'/result',headers=auth,
                          json={'lease_id':claim['lease_id'],'status':'completed','result':result}).status_code==200
        wait_until(lambda:app.state.store.task(parent['id'])['status']=='completed')
        assert 'hello.txt' in app.state.store.task(parent['id'])['result']['text']
        assert app.state.store.rows('SELECT task_id FROM messages WHERE task_id=?',(parent['id'],))


def test_cancel_parent_stops_future_steps(tmp_path):
    app,c=setup(tmp_path)
    with c:
        c.post('/api/login',json={'password':app.state.password_file.read_text()})
        d,auth=pair(c)
        parent=c.post('/api/tasks',json={'prompt':'Two steps','node_id':d['node_id']}).json()
        action=wait_until(lambda:child(app,parent['id']))
        c.post('/api/tasks/'+action['id']+'/approve')
        claim=c.get('/api/node/work',headers=auth).json()['task']
        assert c.post('/api/tasks/'+parent['id']+'/cancel').json()['status']=='cancel_requested'
        assert claim['id'] in c.post('/api/node/heartbeat',headers=auth,json={}).json()['cancelled_task_ids']
        c.post('/api/node/tasks/'+claim['id']+'/result',headers=auth,json={'lease_id':claim['lease_id'],'status':'cancelled'})
        assert app.state.store.task(parent['id'])['status']=='cancelled'
        assert len(app.state.store.rows('SELECT id FROM tasks WHERE parent_id=?',(parent['id'],)))==1


def test_dangerous_permissions_require_owner_review_and_manifest_change_resets(tmp_path):
    app,c=setup(tmp_path)
    with c:
        c.post('/api/login',json={'password':app.state.password_file.read_text()})
        caps=['device.info','command.run','harness.run']
        manifest=[{'id':'pc-agent','name':'PC agent'}]
        d,auth=pair(c,caps,manifest)
        node=c.get('/api/state').json()['nodes'][0]
        assert node['capabilities']==['device.info']
        response=c.post('/api/nodes/'+d['node_id']+'/permissions',json={'capabilities':caps})
        assert response.status_code==200
        c.post('/api/node/heartbeat',headers=auth,json={'capabilities':caps,'harnesses':manifest})
        assert 'harness.run' in c.get('/api/state').json()['nodes'][0]['capabilities']
        c.post('/api/node/heartbeat',headers=auth,json={'capabilities':caps,'harnesses':[{'id':'changed','name':'Changed agent'}]})
        assert c.get('/api/state').json()['nodes'][0]['capabilities']==['device.info']
        assert c.post('/api/node/disconnect',headers=auth,json={}).status_code==200
        assert c.get('/api/node/work',headers=auth).status_code==401


def test_manual_retry_has_new_id_and_unknown_requires_confirmation(tmp_path):
    app,c=setup(tmp_path)
    with c:
        c.post('/api/login',json={'password':app.state.password_file.read_text()})
        d,auth=pair(c)
        t=c.post('/api/tasks',json={'prompt':'task','node_id':d['node_id'],'capability':'device.info'}).json()
        wait_until(lambda:app.state.store.task(t['id'])['status']=='awaiting_approval')
        app.state.store.update_task(t['id'],'outcome_unknown')
        assert c.post('/api/tasks/'+t['id']+'/retry',json={}).status_code==409
        new=c.post('/api/tasks/'+t['id']+'/retry',json={'confirmed_outcome':True}).json()
        assert new['id']!=t['id']


def test_removing_permission_cancels_running_action_and_does_not_revive_it(tmp_path):
    app,c=setup(tmp_path)
    with c:
        c.post('/api/login',json={'password':app.state.password_file.read_text()})
        d,auth=pair(c)
        t=c.post('/api/tasks',json={'prompt':'Read scope','node_id':d['node_id'],'capability':'device.info'}).json()
        wait_until(lambda:app.state.store.task(t['id'])['status']=='awaiting_approval')
        c.post('/api/tasks/'+t['id']+'/approve')
        claim=c.get('/api/node/work',headers=auth).json()['task']
        c.post('/api/nodes/'+d['node_id']+'/permissions',json={'capabilities':[]})
        assert app.state.store.task(t['id'])['status']=='cancel_requested'
        assert t['id'] in c.post('/api/node/heartbeat',headers=auth,json={}).json()['cancelled_task_ids']
        c.post('/api/node/tasks/'+t['id']+'/result',headers=auth,json={'lease_id':claim['lease_id'],'status':'cancelled'})
        c.post('/api/nodes/'+d['node_id']+'/permissions',json={'capabilities':['device.info']})
        assert c.get('/api/node/work',headers=auth).json()['task'] is None
        assert app.state.store.task(t['id'])['status']=='cancelled'
