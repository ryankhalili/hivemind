import json

import pytest

from hivemind.permissions import claim_allowed, clean_scope, grant_scope
from fastapi.testclient import TestClient
from hivemind.app import create_app
from test_workflows import Planner, wait_until


def device(level='folder'):
    return {'scope':{'id':'a'*64,'name':'Shared'},'approved_scope_id':'a'*64,'approval_level':level,
            'capabilities':['device.info','files.list','files.read','files.write','command.run','harness.run','browser.read','browser.open']}


@pytest.mark.parametrize('level', ['ask','read','folder'])
def test_levels_never_grant_sensitive_actions(level):
    for cap,args in [('command.run',{'argv':['echo','hello']}),('harness.run',{'harness_id':'agent','goal':'read'}),('browser.read',{}),('browser.open',{'url':'https://example.com'})]:
        assert grant_scope(device(level),cap,args)==''


def test_folder_grants_are_bounded_and_new_files_only():
    selected=device()
    for cap,args in [('device.info',{}),('files.list',{'path':'.'}),('files.read',{'path':'notes/test.txt'}),('files.write',{'path':'new.txt','content':'hello'})]:
        assert grant_scope(selected,cap,args)=='a'*64
    for path in ['../private.txt','/private.txt','C:/private.txt','private.txt:stream','..\\private.txt','bad\x00.txt']:
        assert not grant_scope(selected,'files.read',{'path':path})
    assert not grant_scope(selected,'files.write',{'path':'new.txt','content':'hello','overwrite':True})
    assert not grant_scope(selected,'files.write',{'path':'new.txt','content':'x'*65537})
    assert not grant_scope(selected,'files.delete',{'path':'new.txt'})
    assert not grant_scope(device('read'),'files.write',{'path':'new.txt','content':'hello'})
    assert not grant_scope(device('ask'),'files.read',{'path':'new.txt'})


def test_grants_require_same_folder_and_enabled_capability():
    selected=device()
    selected['approved_scope_id']='b'*64
    assert not grant_scope(selected,'files.read',{'path':'test.txt'})
    selected=device()
    selected['scope']={}
    assert not grant_scope(selected,'device.info',{})
    selected=device()
    selected['capabilities']=[]
    assert not grant_scope(selected,'files.read',{'path':'test.txt'})
    selected=device()
    selected['scope']=json.dumps(selected['scope'])
    selected['capabilities']=json.dumps(selected['capabilities'])
    assert grant_scope(selected,'files.read',{'path':'test.txt'})=='a'*64


def test_claim_checks_grant_again():
    task={'approval':'grant','approval_scope_id':'a'*64,'capability':'files.read','arguments':{'path':'test.txt'}}
    assert claim_allowed(device(),task)
    assert not claim_allowed(device('ask'),task)
    task['approval_scope_id']='b'*64
    assert not claim_allowed(device(),task)


def test_scope_validation():
    assert clean_scope(None)=={}
    assert clean_scope({'id':'a'*64,'name':'Shared','private_path':'not retained'})=={'id':'a'*64,'name':'Shared'}
    for value in [{'id':'bad','name':'Shared'},{'id':'a'*64,'name':''},{'id':'a'*64,'name':'bad\nfolder'},[],True]:
        with pytest.raises(ValueError):
            clean_scope(value)


@pytest.fixture
def connected(tmp_path):
    app=create_app(tmp_path,Planner())
    with TestClient(app) as client:
        assert client.post('/api/login',json={'password':app.state.password_file.read_text()}).status_code==200
        code=client.post('/api/pairing',json={}).json()['code']
        caps=['device.info','files.list','files.read','files.write','command.run','harness.run']
        paired=client.post('/api/nodes/pair',json={'code':code,'name':'Test computer','kind':'pc','capabilities':caps,
                           'scope':{'id':'a'*64,'name':'Shared'},'harnesses':[{'id':'test','name':'Test agent'}]}).json()
        auth={'Authorization':'Bearer '+paired['token']}
        yield app,client,paired['node_id'],auth,caps


def policy(client,node_id,caps,level,scope_id='a'*64):
    return client.post('/api/nodes/'+node_id+'/permissions',json={'capabilities':caps,'approval_level':level,'scope_id':scope_id})


def action(app,client,node_id,cap='files.read',args=None):
    response=client.post('/api/tasks',json={'prompt':'Perform the requested step','node_id':node_id,'capability':cap,'arguments':{'path':'test.txt'} if args is None else args})
    assert response.status_code==200
    task_id=response.json()['id']
    return wait_until(lambda:(task if (task:=app.state.store.task(task_id))['status']!='planning' else None))


def test_read_grant_queues_reads_but_new_files_still_ask(connected):
    app,c,node_id,auth,caps=connected
    task=action(app,c,node_id)
    assert task['status']=='awaiting_approval'
    assert c.get('/api/node/work',headers=auth).json()['task'] is None
    assert policy(c,node_id,caps,'read').status_code==200
    claimed=c.get('/api/node/work',headers=auth).json()['task']
    assert claimed['id']==task['id'] and claimed['approval']=='grant'
    assert claimed['approval_scope_id']=='a'*64
    assert c.post('/api/node/tasks/'+task['id']+'/result',headers=auth,json={'lease_id':claimed['lease_id'],'status':'completed','result':{'content':'test'}}).status_code==200
    written=action(app,c,node_id,'files.write',{'path':'new.txt','content':'hello'})
    assert written['status']=='awaiting_approval'
    assert any(e['kind']=='approved_by_grant' for e in c.get('/api/tasks/'+task['id']+'/events').json())


def test_folder_grant_runs_whole_workflow_without_step_approvals(connected):
    app,c,node_id,auth,caps=connected
    assert policy(c,node_id,caps,'folder').status_code==200
    parent=c.post('/api/tasks',json={'prompt':'Check the folder and create Hello World','node_id':node_id}).json()
    for cap,result in [('device.info',{'shared_folder':'Shared'}),('files.write',{'created':True,'path':'hello.txt'})]:
        claimed=wait_until(lambda:c.get('/api/node/work',headers=auth).json()['task'])
        assert claimed['capability']==cap and claimed['approval']=='grant'
        assert c.post('/api/node/tasks/'+claimed['id']+'/result',headers=auth,json={'lease_id':claimed['lease_id'],'status':'completed','result':result}).status_code==200
    wait_until(lambda:app.state.store.task(parent['id'])['status']=='completed')


def test_sensitive_action_still_requires_exact_dashboard_approval(connected):
    app,c,node_id,auth,caps=connected
    assert policy(c,node_id,caps,'folder').status_code==200
    task=action(app,c,node_id,'command.run',{'argv':['echo','hello']})
    assert task['status']=='awaiting_approval'
    assert c.get('/api/node/work',headers=auth).json()['task'] is None
    assert c.post('/api/tasks/'+task['id']+'/approve').status_code==200
    claimed=c.get('/api/node/work',headers=auth).json()['task']
    assert claimed['id']==task['id'] and claimed['approval']=='explicit'


def test_downgrade_removes_queued_grant_and_stops_running_grant(connected):
    app,c,node_id,auth,caps=connected
    assert policy(c,node_id,caps,'folder').status_code==200
    task=action(app,c,node_id)
    assert task['status']=='queued'
    assert policy(c,node_id,caps,'ask').status_code==200
    assert c.get('/api/node/work',headers=auth).json()['task'] is None
    assert app.state.store.task(task['id'])['status']=='awaiting_approval'
    assert policy(c,node_id,caps,'read').status_code==200
    claimed=c.get('/api/node/work',headers=auth).json()['task']
    assert claimed['id']==task['id']
    assert policy(c,node_id,caps,'ask').status_code==200
    assert app.state.store.task(task['id'])['status']=='cancel_requested'
    assert task['id'] in c.post('/api/node/heartbeat',headers=auth,json={}).json()['cancelled_task_ids']


def test_folder_change_cancels_old_work_and_stale_grant_is_rejected(connected):
    app,c,node_id,auth,caps=connected
    assert policy(c,node_id,caps,'folder').status_code==200
    task=action(app,c,node_id)
    assert c.post('/api/node/heartbeat',headers=auth,json={'scope':{'id':'b'*64,'name':'Different'}}).status_code==200
    assert app.state.store.task(task['id'])['status']=='cancelled'
    current=c.get('/api/state').json()['nodes'][0]
    assert current['approval_level']=='ask' and current['scope']['id']=='b'*64
    assert policy(c,node_id,[],'folder').status_code==409
    assert c.get('/api/state').json()['nodes'][0]['capabilities']==current['capabilities']
    assert policy(c,node_id,caps,'folder',scope_id='b'*64).status_code==200


def test_claim_rechecks_saved_grant_even_if_queue_was_not_updated(connected):
    app,c,node_id,auth,caps=connected
    assert policy(c,node_id,caps,'folder').status_code==200
    task=action(app,c,node_id)
    app.state.store.execute("UPDATE nodes SET approval_level='ask' WHERE id=?",(node_id,))
    assert c.get('/api/node/work',headers=auth).json()['task'] is None
    assert app.state.store.task(task['id'])['status']=='awaiting_approval'


def test_old_connector_cannot_gain_grant_and_cannot_change_policy(connected):
    app,c,node_id,auth,caps=connected
    assert c.post('/api/node/heartbeat',headers=auth,json={'scope':{}}).status_code==200
    assert policy(c,node_id,caps,'folder',scope_id='').status_code in (400,409)
    assert c.post('/api/node/heartbeat',headers=auth,json={'approval_level':'folder','local_confirmation':False}).status_code==200
    current=c.get('/api/state').json()['nodes'][0]
    assert current['approval_level']=='ask'
    assert current['local_confirmation'] is False
