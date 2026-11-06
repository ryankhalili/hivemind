"""Desktop connector with scoped files and separately enabled local process adapters."""
from __future__ import annotations

import json
import hashlib
import os
import platform
import sqlite3
import stat
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from harness import Cancelled
except ImportError:
    class Cancelled(Exception):
        pass

CAPABILITIES = ["device.info", "files.list", "files.read", "files.write"]
MAX_BYTES = 65536


def is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or (
        os.name == "nt" and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    )


def server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Use the server's address only, without a path or credentials.")
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if not parsed.hostname or parsed.scheme not in ("http", "https") or (parsed.scheme != "https" and not local):
        raise ValueError("Use an HTTPS address. HTTP is supported only on this computer.")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("The server redirected this request. Check its address.")


class Client:
    def __init__(self, server: str, token: str = ""):
        self.server = server_url(server)
        self.token = token
        self.opener = build_opener(NoRedirect)

    def call(self, method: str, path: str, payload=None, timeout=15):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(self.server + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers, method=method)
        with self.opener.open(request, timeout=timeout) as response:
            data = response.read(262145)
            if len(data) > 262144:
                raise ValueError("Server response is too large.")
            return json.loads(data)


class Folder:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Choose a folder to share.")
        self.identity = self._identity()

    def _identity(self):
        stat = self.root.stat()
        return (os.path.normcase(str(self.root)), stat.st_dev, stat.st_ino)

    def scope(self):
        if is_link(self.root) or self._identity() != self.identity:
            raise ValueError("The shared folder changed. Reconnect and review folder access in HiveMind.")
        scope_id = hashlib.sha256(json.dumps(self.identity).encode()).hexdigest()
        return {"id": scope_id, "name": self.root.name or self.root.anchor}

    def path(self, relative: str = ".") -> Path:
        self.scope()
        if not isinstance(relative, str) or len(relative) > 1024 or "\x00" in relative:
            raise ValueError("Invalid path.")
        candidate = Path(relative)
        if candidate.is_absolute() or candidate.drive or ":" in relative or "\\" in relative:
            raise ValueError("Use a relative path with forward slashes.")
        if ".." in candidate.parts:
            raise ValueError("Parent paths are not allowed.")
        unresolved = self.root / candidate
        current = self.root
        for part in candidate.parts:
            current = current / part
            if is_link(current):
                raise ValueError("Links are not shared.")
        resolved = unresolved.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path is outside the shared folder.")
        return resolved

    def execute(self, capability: str, arguments: dict) -> dict:
        self.scope()
        if capability == "device.info":
            return {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "shared_folder": self.root.name}
        if capability not in CAPABILITIES:
            raise ValueError("This connector does not support that action.")
        path = self.path(arguments.get("path", "."))
        if capability == "files.list":
            entries = []
            with os.scandir(path) as iterator:
                for item in iterator:
                    if len(entries) == 200:
                        return {"entries": entries, "truncated": True}
                    if is_link(Path(item.path)):
                        continue
                    entries.append({"name": item.name, "kind": "folder" if item.is_dir(follow_symlinks=False) else "file"})
            return {"entries": sorted(entries, key=lambda x: x["name"]), "truncated": False}
        if capability == "files.read":
            if not path.is_file():
                raise ValueError("Choose a regular text file.")
            with path.open("rb") as source:
                data = source.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("Text files must be 64 KB or smaller.")
            return {"path": arguments.get("path"), "content": data.decode("utf-8")}
        content = arguments.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Provide text of 64 KB or less.")
        if not path.parent.is_dir():
            raise ValueError("The destination folder must already exist.")
        # New files only: overwriting requires a future explicit capability.
        with path.open("xb") as destination:
            destination.write(content.encode("utf-8"))
        return {"path": arguments.get("path"), "bytes_written": len(content.encode("utf-8")), "created": True}


class Journal:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, payload TEXT, acknowledged INTEGER DEFAULT 0)")
        self.db.commit()

    def begin(self, task_id: str) -> bool:
        try:
            self.db.execute("INSERT INTO executions (id) VALUES (?)", (task_id,))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def finish(self, task_id: str, payload: dict):
        self.db.execute("UPDATE executions SET payload=? WHERE id=?", (json.dumps(payload), task_id))
        self.db.commit()

    def pending(self):
        return [(row[0], json.loads(row[1])) for row in self.db.execute("SELECT id,payload FROM executions WHERE acknowledged=0 AND payload IS NOT NULL")]

    def acknowledge(self, task_id: str):
        self.db.execute("UPDATE executions SET acknowledged=1 WHERE id=?", (task_id,))
        self.db.commit()


class Node:
    def __init__(self, client: Client, folder: Folder, journal: Journal, status=print, runner=None):
        self.client, self.folder, self.journal, self.status = client, folder, journal, status
        self.runner = runner
        self.stop = threading.Event()
        self.active_cancel = None
        self.active_task = None

    def disconnect(self):
        self.stop.set()
        if self.active_cancel:
            self.active_cancel.set()

    def manifest(self):
        return {"capabilities":CAPABILITIES + (self.runner.capabilities() if self.runner else []),
                "harnesses":self.runner.manifest() if self.runner else [],
                "scope":self.folder.scope(),
                "local_confirmation":not (self.runner and self.runner.remote_approval)}

    def heartbeat(self):
        return self.client.call("POST", "/api/node/heartbeat", self.heartbeat_payload())

    def heartbeat_payload(self):
        payload=self.manifest()
        if self.active_task:
            payload["active_task"]=dict(self.active_task)
        return payload

    def flush(self):
        for task_id, payload in self.journal.pending():
            try:
                self.client.call("POST", f"/api/node/tasks/{task_id}/result", payload)
                self.journal.acknowledge(task_id)
            except HTTPError as error:
                if error.code in (404, 409, 410):
                    self.journal.acknowledge(task_id)
                    self.status("Result arrived too late. Review the task in HiveMind; it will not run again.")
                else:
                    raise

    def tick(self):
        heartbeat = self.heartbeat()
        self.flush()
        if self.stop.is_set():
            return
        task = self.client.call("GET", "/api/node/work").get("task")
        if not task:
            return
        if not self.journal.begin(task["id"]):
            self.status("Skipped a previously claimed task. Review its result in HiveMind.")
            return
        payload = {"lease_id": task["lease_id"], "status": "completed"}
        self.active_task={"id":task["id"],"lease_id":task["lease_id"]}
        cancel = threading.Event()
        self.active_cancel = cancel
        done = threading.Event()
        def progress(message):
            self.status(message)
            try:
                self.client.call("POST",f"/api/node/tasks/{task['id']}/progress",{"lease_id":task["lease_id"],"message":message},timeout=2)
            except Exception:
                pass
        def monitor():
            # Separate transport so in-flight requests do not share an opener.
            transport = Client(self.client.server,self.client.token)
            failures = 0
            while not done.wait(1):
                if self.stop.is_set():
                    cancel.set()
                    return
                try:
                    response = transport.call("POST","/api/node/heartbeat",self.heartbeat_payload())
                    failures = 0
                    if task["id"] in response.get("cancelled_task_ids",[]):
                        cancel.set()
                        return
                except HTTPError as error:
                    if error.code in (401,403):
                        cancel.set()
                        self.stop.set()
                        return
                    failures += 1
                except Exception:
                    failures += 1
                if failures >= 3:
                    cancel.set()
                    return
                done.wait(2)
        monitor_thread = None
        try:
            if task["id"] in heartbeat.get("cancelled_task_ids", []) or self.stop.is_set():
                raise Cancelled("Task cancelled before execution.")
            # Recheck cancellation immediately before the bounded operation.
            current = self.heartbeat()
            if task["id"] in current.get("cancelled_task_ids", []):
                raise Cancelled("Task cancelled before execution.")
            self.status("Running " + task["capability"])
            if task["capability"] in ("command.run","harness.run"):
                if self.runner is None:
                    raise ValueError("Local process execution is disabled.")
                monitor_thread = threading.Thread(target=monitor,daemon=True)
                monitor_thread.start()
                payload["result"] = self.runner.execute(task["capability"],task.get("arguments",{}),cancel,progress,
                                                       dashboard_approved=task.get("approval") == "explicit")
                if payload["result"].get("exit_code",0) != 0:
                    payload.update(status="failed",error="Local process exited with a nonzero code. Inspect its output.")
            else:
                if task.get("approval") == "grant" and task.get("approval_scope_id") != self.folder.scope()["id"]:
                    raise ValueError("This action was granted access to a different folder. Review folder access in HiveMind.")
                payload["result"] = self.folder.execute(task["capability"], task.get("arguments", {}))
        except Cancelled as error:
            payload.update(status="cancelled",error=str(error))
        except Exception as error:
            payload.update(status="failed", error=error.__class__.__name__ + ": " + str(error)[:300])
        finally:
            done.set()
            self.active_cancel = None
            self.active_task = None
            if monitor_thread:
                monitor_thread.join(timeout=1)
        self.journal.finish(task["id"], payload)
        self.flush()
        self.status("Connected. Waiting for an approved task.")

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except HTTPError as error:
                if error.code in (401, 403):
                    self.status("This connection was revoked or expired. Pair again in HiveMind.")
                    break
                self.status("Server unavailable. Retrying the connection; actions will not repeat.")
            except Exception:
                self.status("Connection interrupted. Reconnecting…")
            self.stop.wait(2)
        self.journal.db.close()
