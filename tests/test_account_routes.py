from fastapi.testclient import TestClient
import pytest
from hivemind.app import create_app
from hivemind.providers import MockProvider


def test_account_routes_require_owner(tmp_path):
    with TestClient(create_app(tmp_path, MockProvider())) as client:
        for path in ('start','cancel'):
            assert client.post('/api/providers/login/'+path,json={}).status_code==401
        assert client.get('/api/providers/login/status').status_code==401


def test_account_activation_requires_completed_login(tmp_path):
    app=create_app(tmp_path,MockProvider())
    with TestClient(app) as client:
        client.post('/api/login',json={'password':app.state.password_file.read_text()})
        assert client.get('/api/providers/login/status').json()['status']=='idle'
        response=client.post('/api/providers',json={'type':'hermes-account','hermes_home':'/tmp/foreign-profile'})
        assert response.status_code==409
        assert not (tmp_path/'provider.enc').exists()
        assert client.post('/api/providers/login/start',json={'model':'bad model; command'}).status_code==400
        assert client.post('/api/providers/login/cancel',json={}).status_code==200


@pytest.mark.parametrize('path', ['/api/login','/api/memories','/api/providers'])
def test_non_object_json_is_rejected(tmp_path,path):
    app=create_app(tmp_path,MockProvider())
    with TestClient(app) as client:
        client.post('/api/login',json={'password':app.state.password_file.read_text()})
        assert client.post(path,json=[]).status_code==400
