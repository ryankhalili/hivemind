import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/"connectors"/"desktop"))
from harness import Cancelled, OUTPUT_LIMIT, Runner, run_process
from node import Folder
import node as desktop_node


def approved(tmp_path, **kwargs):
    return Runner(Folder(tmp_path),confirm=lambda details,cancel:True,**kwargs)


def test_commands_disabled_by_default(tmp_path):
    with pytest.raises(ValueError,match="disabled"):
        approved(tmp_path).execute("command.run",{"argv":[sys.executable,"-c","print('no')"]},threading.Event())


def test_commands_need_local_confirmation(tmp_path):
    runner=Runner(Folder(tmp_path),commands=True)
    with pytest.raises(Cancelled):
        runner.execute("command.run",{"argv":[sys.executable,"-c","print('no')"]},threading.Event())


def test_dashboard_approval_needs_local_opt_in(tmp_path):
    runner=Runner(Folder(tmp_path),commands=True)
    args={"argv":[sys.executable,"-c","print('approved')"]}
    with pytest.raises(Cancelled):
        runner.execute("command.run",args,threading.Event(),dashboard_approved=True)
    remote=Runner(Folder(tmp_path),commands=True,remote_approval=True)
    with pytest.raises(Cancelled):
        remote.execute("command.run",args,threading.Event(),dashboard_approved=False)
    assert remote.execute("command.run",args,threading.Event(),dashboard_approved=True)["stdout"].strip()=="approved"
    cancelled=threading.Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        remote.execute("command.run",args,cancelled,dashboard_approved=True)


def test_command_exec_and_argument_boundaries(tmp_path):
    runner=approved(tmp_path,commands=True)
    result=runner.execute("command.run",{"argv":[sys.executable,"-c","import sys; print(sys.argv[1])","hello & echo SHOULD_NOT_RUN"]},threading.Event())
    assert result["exit_code"]==0
    assert result["stdout"].strip()=="hello & echo SHOULD_NOT_RUN"
    for change in [{"timeout":301},{"timeout":True},{"cwd":"../"},{"argv":"echo unsafe"},{"argv":["script.cmd"]}]:
        with pytest.raises(ValueError):
            runner.prepare("command.run",{"argv":[sys.executable],**change})


def test_named_harness_json_and_no_cloud_executable(tmp_path):
    adapter={"name":"Test","executable":sys.executable,"arguments":["-c","import json,sys; d=json.load(sys.stdin); print(json.dumps({'received':d['goal']}))"]}
    runner=approved(tmp_path,harnesses={"test":adapter})
    result=runner.execute("harness.run",{"harness_id":"test","goal":"Read only the supplied goal"},threading.Event())
    assert result["output"]=={"received":"Read only the supplied goal"}
    assert result["stdout"]==""
    with pytest.raises(ValueError,match="only be changed"):
        runner.prepare("harness.run",{"harness_id":"test","goal":"hi","executable":"attacker.exe"})
    with pytest.raises(ValueError,match="registered"):
        runner.prepare("harness.run",{"harness_id":"unknown","goal":"hi"})
    assert "executable" not in runner.manifest()[0]


def test_process_output_is_bounded(tmp_path):
    result=run_process([sys.executable,"-c","import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*100000)"],tmp_path,10,threading.Event())
    assert result["truncated"]
    assert len(result["stdout"])+len(result["stderr"])<=OUTPUT_LIMIT


def test_process_timeout(tmp_path):
    start=time.monotonic()
    with pytest.raises(TimeoutError):
        run_process([sys.executable,"-c","import time; time.sleep(60)"],tmp_path,1,threading.Event())
    assert time.monotonic()-start<10


def process_alive(pid):
    if os.name=="nt":
        import ctypes
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return False
        status=ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle,ctypes.byref(status))
        kernel.CloseHandle(handle)
        return status.value==259
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False


def test_cancel_stops_child_process_tree(tmp_path):
    pid_file=tmp_path/"child.pid"
    code="import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    cancel=threading.Event()
    errors=[]
    def execute():
        try:
            run_process([sys.executable,"-c",code,str(pid_file)],tmp_path,30,cancel)
        except Exception as error:
            errors.append(error)
    thread=threading.Thread(target=execute)
    thread.start()
    for _ in range(100):
        if pid_file.exists():
            break
        time.sleep(.05)
    assert pid_file.exists()
    child=int(pid_file.read_text())
    cancel.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0],Cancelled)
    for _ in range(20):
        if not process_alive(child):
            break
        time.sleep(.1)
    assert not process_alive(child)


def test_runtime_does_not_inherit_node_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv("HIVE_NODE_TOKEN","must-not-pass")
    result=run_process([sys.executable,"-c","import os; print('present' if 'HIVE_NODE_TOKEN' in os.environ else 'absent')"],tmp_path,10,threading.Event())
    assert result["stdout"].strip()=="absent"


def test_harness_revision_freezes_settings_and_rejects_changed_script(tmp_path):
    script=tmp_path/"adapter.py"
    script.write_text("print('first')")
    registration={"agent":{"name":"Agent","executable":sys.executable,"arguments":[str(script)]}}
    runner=approved(tmp_path,harnesses=registration)
    revision=runner.manifest()[0]["revision"]
    assert len(revision)==64
    registration["agent"]["arguments"]=["unapproved.py"]
    assert runner.harnesses["agent"]["arguments"]==[str(script)]
    script.write_text("print('changed')")
    with pytest.raises(ValueError,match="Reconnect"):
        runner.prepare("harness.run",{"harness_id":"agent","goal":"hello"})
    replacement=approved(tmp_path,harnesses={"agent":{"executable":sys.executable,"arguments":[str(script)]}})
    assert replacement.manifest()[0]["revision"]!=revision


def test_relative_adapter_script_is_bound_before_cloud_selects_cwd(tmp_path):
    (tmp_path/"adapter.py").write_text("print('registered')")
    (tmp_path/"other").mkdir()
    (tmp_path/"other"/"adapter.py").write_text("print('different')")
    runner=approved(tmp_path,harnesses={"agent":{"executable":sys.executable,"arguments":["adapter.py"]}})
    argv,*_=runner.prepare("harness.run",{"harness_id":"agent","goal":"hello","cwd":"other"})
    assert argv[1]==str((tmp_path/"adapter.py").resolve())


def test_node_keeps_heartbeats_and_acknowledges_remote_cancel(tmp_path,monkeypatch):
    remote_cancel=threading.Event()
    task={"id":"process-task","lease_id":"lease","capability":"command.run", "arguments":{"argv":[sys.executable,"-c","import time; time.sleep(30)"]}}
    class FakeClient:
        server="https://example.invalid"
        token="unused"
        heartbeats=0
        heartbeat_payloads=[]
        progress=[]
        result=None
        def __init__(self,*args):
            pass
        def call(self,method,path,payload=None,timeout=15):
            if path.endswith("heartbeat"):
                FakeClient.heartbeats+=1
                FakeClient.heartbeat_payloads.append(payload)
                return {"cancelled_task_ids":[task["id"]] if remote_cancel.is_set() else []}
            if path.endswith("work"):
                return {"task":task}
            if path.endswith("progress"):
                FakeClient.progress.append(payload["message"])
                return {"ok":True}
            FakeClient.result=payload
            return {"ok":True}
    monkeypatch.setattr(desktop_node,"Client",FakeClient)
    journal=desktop_node.Journal(tmp_path/"journal.sqlite3")
    worker=desktop_node.Node(FakeClient(),Folder(tmp_path),journal,lambda _:None,approved(tmp_path,commands=True))
    timer=threading.Timer(.5,remote_cancel.set)
    timer.start()
    worker.tick()
    timer.join()
    assert FakeClient.heartbeats>=3
    assert FakeClient.progress
    assert FakeClient.result["status"]=="cancelled"
    assert any(item.get("active_task")=={"id":"process-task","lease_id":"lease"} for item in FakeClient.heartbeat_payloads)
    assert "active_task" not in worker.heartbeat_payload()
    journal.db.close()
