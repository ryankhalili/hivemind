import importlib.util
import json
import os
import subprocess
import sys
import queue
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest

MODULE = Path(__file__).parents[1] / "connectors" / "desktop" / "node.py"
sys.path.insert(0,str(MODULE.parent))
spec = importlib.util.spec_from_file_location("desktop_node", MODULE)
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


def test_folder_read_write_and_scope(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    folder = node.Folder(shared)
    assert folder.execute("files.write", {"path": "hello.txt", "content": "hello"})["created"]
    assert folder.execute("files.read", {"path": "hello.txt"})["content"] == "hello"
    assert folder.execute("files.list", {})["entries"] == [{"name": "hello.txt", "kind": "file"}]
    for path in ["../private.txt", str(tmp_path / "private.txt"), "C:/private.txt", "hello.txt:secret", "..\\private.txt"]:
        with pytest.raises(ValueError):
            folder.execute("files.read", {"path": path})
    with pytest.raises(FileExistsError):
        folder.execute("files.write", {"path": "hello.txt", "content": "replacement"})
    with pytest.raises(ValueError):
        folder.execute("shell.run", {"command": "whoami"})


def test_folder_rejects_symlinks(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("private")
    try:
        (shared / "link.txt").symlink_to(secret)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")
    with pytest.raises(ValueError, match="Links"):
        node.Folder(shared).execute("files.read", {"path": "link.txt"})


def test_size_limits(tmp_path):
    folder = node.Folder(tmp_path)
    (tmp_path / "large.txt").write_bytes(b"a" * (node.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        folder.execute("files.read", {"path": "large.txt"})
    with pytest.raises(ValueError):
        folder.execute("files.write", {"path": "new.txt", "content": "a" * (node.MAX_BYTES + 1)})
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction check")
def test_folder_rejects_windows_junction(tmp_path):
    shared = tmp_path / "shared"
    outside = tmp_path / "outside"
    shared.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("private")
    link = shared / "linked"
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "New-Item -ItemType Junction -Path " + quote(link) + " -Target " + quote(outside) + " | Out-Null"], check=True, capture_output=True)
    with pytest.raises(ValueError):
        node.Folder(shared).execute("files.read", {"path": "linked/secret.txt"})
    assert node.Folder(shared).execute("files.list", {})["entries"] == []


def test_server_requires_tls_and_no_redirect_credentials():
    assert node.server_url("https://example.com/") == "https://example.com"
    assert node.server_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    for url in ["http://example.com", "https://user:password@example.com", "https://example.com/path", "file:///tmp"]:
        with pytest.raises(ValueError):
            node.server_url(url)


def test_lost_ack_never_repeats_write(tmp_path):
    task = {"id": "task-one", "lease_id": "lease-one", "capability": "files.write", "arguments": {"path": "result.txt", "content": "once"}}
    class Client:
        failures = 1
        results = []
        def call(self, method, path, payload=None):
            if path.endswith("/heartbeat"):
                return {"cancelled_task_ids": []}
            if path.endswith("/work"):
                return {"task": task}
            if self.failures:
                self.failures -= 1
                raise OSError("Lost acknowledgment")
            self.results.append(payload)
            return {"ok": True}
    client = Client()
    journal = node.Journal(tmp_path / "journal.sqlite3")
    worker = node.Node(client, node.Folder(tmp_path), journal, lambda _: None)
    with pytest.raises(OSError):
        worker.tick()
    assert (tmp_path / "result.txt").read_text() == "once"
    worker.tick()
    assert len(client.results) == 1
    assert client.results[0]["status"] == "completed"
    assert journal.pending() == []
    journal.db.close()


def test_journal_survives_restart(tmp_path):
    location = tmp_path / "journal.sqlite3"
    journal = node.Journal(location)
    assert journal.begin("claimed-before-crash")
    journal.db.close()
    reopened = node.Journal(location)
    assert not reopened.begin("claimed-before-crash")
    reopened.db.close()


def test_cancelled_task_does_not_write(tmp_path):
    task = {"id": "cancelled", "lease_id": "lease", "capability": "files.write", "arguments": {"path": "no.txt", "content": "no"}}
    class Client:
        result = None
        def call(self, method, path, payload=None):
            if path.endswith("/heartbeat"):
                return {"cancelled_task_ids": ["cancelled"]}
            if path.endswith("/work"):
                return {"task": task}
            self.result = payload
            return {"ok": True}
    client = Client()
    journal = node.Journal(tmp_path / "journal.sqlite3")
    node.Node(client, node.Folder(tmp_path), journal, lambda _: None).tick()
    assert client.result["status"] == "cancelled"
    assert not (tmp_path / "no.txt").exists()
    journal.db.close()


def desktop_window_without_tk(tmp_path):
    import app as desktop_app
    class Value:
        def __init__(self,value=""):
            self.value=value
        def get(self):
            return self.value
        def set(self,value):
            self.value=value
    class Widget:
        def __init__(self):
            self.options={}
        def configure(self,**kwargs):
            self.options.update(kwargs)
    class Root:
        destroyed=False
        def after(self,*args):
            pass
        def destroy(self):
            self.destroyed=True
    window=desktop_app.App.__new__(desktop_app.App)
    window.root=Root()
    window.folder=tmp_path
    window.config_path=tmp_path/"connection.json"
    window.config={"server":"https://example.invalid","token":"test-token","node_id":"test-node","folder":str(tmp_path),"reconnect":True}
    window.config_lock=threading.RLock()
    window.remote_approval_server=""
    window.messages=queue.Queue()
    window.node=None
    window.thread=None
    window.starting=False
    window.closing=False
    window.forgetting=False
    window.pairing_uncertain=False
    window.stop_requested=threading.Event()
    window.revocation_requested=threading.Event()
    for key,value in {"server":"https://example.invalid","code":"","name":"Test","shared":str(tmp_path),"commands":False,"remote_approval":False,"custom":False,"hermes":False,"reconnect":True,"startup":False,"adapter_name":"Test","adapter_executable":"","adapter_arguments":"[]","status":"Ready"}.items():
        setattr(window,key,Value(value))
    for key in ("connect_button","disconnect_button","forget_button"):
        setattr(window,key,Widget())
    return desktop_app,window


def drain_until(window,predicate,timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        window.drain()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Desktop state did not arrive")


def test_desktop_disconnect_revokes_and_clears_token(tmp_path,monkeypatch):
    desktop_app,window=desktop_window_without_tk(tmp_path)
    calls=[]
    class Client:
        def __init__(self,server,token):
            self.token=token
        def call(self,method,path,payload=None,timeout=15):
            calls.append((method,path,self.token))
            return {"ok":True}
    monkeypatch.setattr(desktop_app,"Client",Client)
    window.disconnect()
    drain_until(window,lambda:not window.forgetting)
    assert calls==[("POST","/api/node/disconnect","test-token")]
    assert "token" not in window.config and "node_id" not in window.config
    assert not window.config["reconnect"]
    assert window.config["folder"]==str(tmp_path)
    assert "Pair again" in window.status.get()


def test_desktop_disconnect_offline_preserves_only_retry_path(tmp_path,monkeypatch):
    desktop_app,window=desktop_window_without_tk(tmp_path)
    class Client:
        def __init__(self,*args):
            pass
        def call(self,*args,**kwargs):
            raise OSError("offline")
    monkeypatch.setattr(desktop_app,"Client",Client)
    window.disconnect()
    drain_until(window,lambda:not window.forgetting)
    assert window.config["token"]=="test-token"
    assert window.config["revocation_pending"] and not window.config["reconnect"]
    assert window.connect_button.options["state"]=="disabled"
    assert window.disconnect_button.options["text"]=="Retry disconnect"
    assert "pending" in window.status.get()
    window.connect()
    assert window.thread is None


def test_desktop_close_only_pauses_without_revocation(tmp_path,monkeypatch):
    desktop_app,window=desktop_window_without_tk(tmp_path)
    monkeypatch.setattr(desktop_app,"Client",lambda *args:pytest.fail("Closing must not revoke"))
    window.close()
    assert window.stop_requested.is_set()
    assert window.config["token"]=="test-token"
    assert not window.revocation_requested.is_set()
    assert window.root.destroyed


def test_disconnect_during_pairing_revokes_late_credential(tmp_path,monkeypatch):
    desktop_app,window=desktop_window_without_tk(tmp_path)
    window.config={}
    window.code.set("PAIR-CODE")
    paired=threading.Event()
    release=threading.Event()
    revoked=[]
    class Client:
        def __init__(self,server,token=""):
            self.token=token
        def call(self,method,path,payload=None,timeout=15):
            if path.endswith("/pair"):
                paired.set()
                assert release.wait(3)
                return {"token":"late-token","node_id":"late-node"}
            if path.endswith("/disconnect"):
                revoked.append(self.token)
                return {"ok":True}
            raise AssertionError("A stopped connection must not heartbeat or run work")
    monkeypatch.setattr(desktop_app,"Client",Client)
    window.connect()
    assert paired.wait(2)
    window.disconnect()
    release.set()
    drain_until(window,lambda:not window.forgetting)
    assert revoked==["late-token"]
    assert "token" not in window.config and "revocation_pending" not in window.config
    assert not window.config["reconnect"]


def test_local_confirmation_has_finite_expiry(tmp_path,monkeypatch):
    desktop_app,window=desktop_window_without_tk(tmp_path)
    monkeypatch.setattr(desktop_app,"CONFIRM_SECONDS",.05)
    cancel=threading.Event()
    started=time.monotonic()
    assert window.confirm("harmless test command",cancel) is False
    assert cancel.is_set() and time.monotonic()-started<1


def test_saved_credential_cannot_move_to_another_server(tmp_path,monkeypatch):
    desktop_app,window=desktop_window_without_tk(tmp_path)
    errors=[]
    monkeypatch.setattr(desktop_app.messagebox,"showerror",lambda title,message:errors.append(message))
    window.server.set("https://different.invalid")
    window.code.set("NEW-CODE")
    window.connect()
    assert window.thread is None
    assert errors and "Disconnect the saved device" in errors[0]
    assert window.config["server"]=="https://example.invalid"


def test_folder_grant_identity_is_stable_but_changes_with_root(tmp_path):
    first=tmp_path/"first"
    second=tmp_path/"second"
    first.mkdir()
    second.mkdir()
    folder=node.Folder(first)
    scope=folder.scope()
    assert len(scope["id"])==64 and scope["name"]=="first"
    assert scope==node.Folder(first).scope()
    folder.execute("files.write",{"path":"test.txt","content":"test"})
    assert folder.scope()==scope
    assert node.Folder(second).scope()["id"]!=scope["id"]
    first.rename(tmp_path/"prior")
    first.mkdir()
    with pytest.raises(ValueError,match="folder changed"):
        folder.execute("files.list",{})


def test_connector_refuses_grant_bound_to_another_folder(tmp_path):
    task={"id":"wrong-folder","lease_id":"lease","capability":"files.write","arguments":{"path":"no.txt","content":"no"},"approval":"grant","approval_scope_id":"f"*64}
    class Client:
        result=None
        def call(self,method,path,payload=None):
            if path.endswith("heartbeat"):
                return {"cancelled_task_ids":[]}
            if path.endswith("work"):
                return {"task":task}
            self.result=payload
            return {"ok":True}
    client=Client()
    journal=node.Journal(tmp_path/"journal.sqlite3")
    node.Node(client,node.Folder(tmp_path),journal,lambda _:None).tick()
    assert client.result["status"]=="failed"
    assert not (tmp_path/"no.txt").exists()
    journal.db.close()


def test_remote_approval_choice_does_not_follow_another_server(tmp_path,monkeypatch):
    desktop_app,window=desktop_window_without_tk(tmp_path)
    window.config.pop("token")
    window.config.pop("node_id")
    window.remote_approval.set(True)
    window.remote_approval_server="https://example.invalid"
    window.server.set("https://different.invalid")
    window.code.set("NEW-CODE")
    errors=[]
    monkeypatch.setattr(desktop_app.messagebox,"showerror",lambda title,message:errors.append(message))
    window.connect()
    assert window.thread is None and window.remote_approval.get() is False
    assert "server address changed" in errors[0]
