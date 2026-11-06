"""Real desktop execution against an isolated application, with deterministic planning."""
import json
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from hivemind.app import create_app

sys.path.insert(0,str(Path(__file__).parents[1]/"connectors"/"desktop"))
import node as desktop_node
from harness import Runner


class Planner:
    def status(self):
        return {"provider":"integration-test"}

    async def reply(self,messages,system):
        data=json.loads(messages[-1]["content"])
        completed=data["completed_steps"]
        device=data["devices"][0]["id"]
        steps=[("device.info",{}),("files.write",{"path":"hello.txt","content":"Hello World"}),
               ("files.read",{"path":"hello.txt"})]
        if len(completed)==len(steps):
            return json.dumps({"done":"Created and read hello.txt: Hello World"})
        capability,arguments=steps[len(completed)]
        return json.dumps({"capability":capability,"node_id":device,"arguments":arguments})


def wait_for(predicate, timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError("Expected application state did not arrive")


def test_real_node_workflow_command_harness_and_cancel(tmp_path,monkeypatch):
    shared=tmp_path/"shared"
    shared.mkdir()
    adapter=tmp_path/"adapter.py"
    adapter.write_text("import json,sys\nrequest=json.load(sys.stdin)\nprint(json.dumps({'goal':request['goal']}))\n")
    confirmations=[]
    runner=Runner(desktop_node.Folder(shared),commands=True,
                  harnesses={"test":{"name":"Test adapter","executable":sys.executable,"arguments":[str(adapter)]}},
                  confirm=lambda details,cancel:confirmations.append(details) or True)
    app=create_app(tmp_path/"server",Planner())
    with TestClient(app) as client:
        assert client.post("/api/login",json={"password":app.state.password_file.read_text()}).status_code==200
        code=client.post("/api/pairing",json={}).json()["code"]
        offered=desktop_node.CAPABILITIES+runner.capabilities()
        paired=client.post("/api/nodes/pair",json={"code":code,"name":"Temporary integration node","kind":"pc",
                           "capabilities":offered,"harnesses":runner.manifest()}).json()
        class Transport:
            def __init__(self,server="https://testserver",token=paired["token"]):
                self.server,self.token=server,token
            def call(self,method,path,payload=None,timeout=15):
                response=client.request(method,path,json=payload,headers={"Authorization":"Bearer "+self.token})
                if response.status_code>=400:
                    raise HTTPError(self.server+path,response.status_code,"Test API rejected request",{},None)
                return response.json()
        monkeypatch.setattr(desktop_node,"Client",Transport)
        journal=desktop_node.Journal(tmp_path/"journal.sqlite3")
        worker=desktop_node.Node(Transport(),desktop_node.Folder(shared),journal,lambda _:None,runner)
        worker.heartbeat()
        node_id=paired["node_id"]
        effective=next(item for item in client.get("/api/state").json()["nodes"] if item["id"]==node_id)["capabilities"]
        assert "command.run" not in effective and "harness.run" not in effective
        assert client.post(f"/api/nodes/{node_id}/permissions",json={"capabilities":offered}).status_code==200
        parent=client.post("/api/tasks",json={"prompt":"Check this computer, create hello.txt with Hello World, and read it back.","node_id":node_id}).json()
        for _ in range(3):
            action=wait_for(lambda:next((row for row in app.state.store.rows("SELECT id FROM tasks WHERE parent_id=? AND status='awaiting_approval'",(parent["id"],))),None))
            assert worker.client.call("GET","/api/node/work")["task"] is None
            assert client.post(f"/api/tasks/{action['id']}/approve",json={}).status_code==200
            worker.tick()
            assert app.state.store.task(action["id"])["status"]=="completed"
        wait_for(lambda:app.state.store.task(parent["id"])["status"]=="completed")
        assert (shared/"hello.txt").read_text()=="Hello World"

        def direct(capability,arguments):
            response=client.post("/api/tasks",json={"prompt":"Temporary integration check","node_id":node_id,"capability":capability,"arguments":arguments})
            assert response.status_code==200
            task=response.json()
            wait_for(lambda:app.state.store.task(task["id"])["status"]=="awaiting_approval")
            assert client.post(f"/api/tasks/{task['id']}/approve",json={}).status_code==200
            return task["id"]
        command=direct("command.run",{"argv":[sys.executable,"-c","print('harmless command')"],"timeout":10})
        worker.tick()
        assert app.state.store.task(command)["result"]["stdout"].strip()=="harmless command"
        harness=direct("harness.run",{"harness_id":"test","goal":"Return this approved goal","timeout":10})
        worker.tick()
        assert app.state.store.task(harness)["result"]["output"]=={"goal":"Return this approved goal"}
        assert len(confirmations)==2
        assert client.get(f"/api/tasks/{harness}/events").status_code==200
        running=direct("command.run",{"argv":[sys.executable,"-c","import time; time.sleep(30)"],"timeout":20})
        timer=threading.Timer(.5,lambda:client.post(f"/api/tasks/{running}/cancel",json={}))
        timer.start()
        worker.tick()
        timer.join()
        assert app.state.store.task(running)["status"]=="cancelled"
        assert worker.client.call("POST","/api/node/disconnect",{})["ok"]
        assert client.post("/api/node/heartbeat",json={},headers={"Authorization":"Bearer "+paired["token"]}).status_code==401
        journal.db.close()
