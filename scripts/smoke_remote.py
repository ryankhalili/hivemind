"""Run one real multi-step workflow on an isolated, temporary desktop node."""
import argparse
import http.cookiejar
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connectors" / "desktop"))
from node import CAPABILITIES, Client, Folder, Journal, Node

TERMINAL = {"completed", "failed", "cancelled", "outcome_unknown"}
FILENAME = "hello-world.txt"
CONTENT = "Hello World"

# Only exact test-task relationships identify records for archival.
ARCHIVE = """
import json, re, sqlite3, sys
from pathlib import Path
data = json.load(sys.stdin)
assert re.fullmatch(r'e2e-v02-[0-9a-f]{32}', data['marker'])
assert re.fullmatch(r'[0-9a-f]{32}', data['parent_id'])
assert re.fullmatch(r'[0-9a-f]{32}', data['node_id'])
db = sqlite3.connect(Path.home() / 'hivemind/.state/hivemind.db', timeout=15)
db.row_factory = sqlite3.Row
with db:
    db.execute('BEGIN IMMEDIATE')
    root = db.execute('SELECT * FROM tasks WHERE id=?', (data['parent_id'],)).fetchone()
    assert root and root['node_id'] == data['node_id'] and data['marker'] in root['prompt']
    assert root['is_workflow'] and not root['parent_id']
    rows = db.execute('''WITH RECURSIVE owned(id) AS (
        SELECT id FROM tasks WHERE id=? UNION ALL
        SELECT tasks.id FROM tasks JOIN owned ON tasks.parent_id=owned.id
    ) SELECT tasks.id,tasks.status,tasks.node_id FROM tasks JOIN owned ON tasks.id=owned.id''',
        (data['parent_id'],)).fetchall()
    assert all(r['node_id'] == data['node_id'] for r in rows)
    assert all(r['status'] in ('completed','failed','cancelled','outcome_unknown') for r in rows)
    ids = sorted(r['id'] for r in rows)
    assert set(data['observed_task_ids']) <= set(ids)
    placeholders = ','.join('?' for _ in ids)
    message_ids = [r['id'] for r in db.execute(
        'SELECT id FROM messages WHERE task_id IN ('+placeholders+')', ids)]
    db.execute('UPDATE tasks SET archived=1 WHERE id IN ('+placeholders+')', ids)
    db.execute('UPDATE messages SET archived=1 WHERE task_id IN ('+placeholders+')', ids)
print(json.dumps({'task_ids':ids,'message_ids':message_ids,'archived':True}))
db.close()
"""


def check_action(task, node_id, parent_id, completed):
    """Approve only bounded actions requested by this particular test."""
    if task.get("node_id") != node_id or task.get("parent_id") != parent_id:
        raise ValueError("Action did not target the test node and workflow")
    cap, args = task.get("capability"), task.get("arguments")
    prior = [item["capability"] for item in completed]
    if cap == "device.info" and args == {}:
        return
    if cap == "files.list" and args == {"path": "."} and "device.info" in prior:
        return
    if cap == "files.write" and args == {"path": FILENAME, "content": CONTENT}:
        if "device.info" in prior and "files.list" in prior and "files.write" not in prior:
            return
    if cap == "files.read" and args == {"path": FILENAME} and "files.write" in prior:
        return
    raise ValueError("Planner proposed an action outside the test's approval scope")


class TestDevice(Client):
    """Check the claimed action again before Node can execute it."""
    def __init__(self, server):
        super().__init__(server)
        self.approved = {}

    def call(self, method, path, payload=None, timeout=15):
        response = super().call(method, path, payload, timeout)
        if path == "/api/node/work" and response.get("task"):
            task = response["task"]
            expected = self.approved.get(task["id"])
            actual = {key: task.get(key) for key in ("node_id", "parent_id", "capability", "arguments")}
            if expected != actual:
                raise ValueError("Claimed action did not match this test's exact approval")
        return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssh-host", required=True, help="SSH alias for a server installed in ~/hivemind")
    parser.add_argument("--password-file", type=Path, help="Local owner password file; otherwise reads the remote bootstrap file")
    parser.add_argument("--folder-grant", action="store_true", help="Test one standing folder grant instead of approving each step")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 60 <= args.timeout <= 1800:
        parser.error("Use a port from 1024-65535 and a timeout from 60-1800 seconds.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", args.ssh_host):
        parser.error("Use a configured SSH host alias.")
    marker = "e2e-v02-" + uuid.uuid4().hex
    run_dir = ROOT / ".state" / marker
    shared = run_dir / "shared"
    shared.mkdir(parents=True)
    base = f"http://127.0.0.1:{args.port}"
    hidden = {"creationflags": 0x08000000} if os.name == "nt" else {}
    ssh_options = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    ssh = subprocess.Popen(ssh_options + ["-N", "-o", "ExitOnForwardFailure=yes", "-L",
                           f"127.0.0.1:{args.port}:127.0.0.1:8787", args.ssh_host],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden)
    session = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def api(method, path, payload=None):
        request = Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                          method=method, headers={"Content-Type": "application/json"})
        with session.open(request, timeout=30) as response:
            return json.load(response)

    node_id = parent_id = journal = None
    known_ids = set()
    report = {"marker": marker, "passed": False, "archived": False}
    stage = "connection"
    try:
        for _ in range(30):
            if ssh.poll() is not None:
                raise RuntimeError("SSH tunnel failed")
            try:
                health = api("GET", "/health")
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("Service unavailable")
        if not str(health.get("version", "")).startswith(("0.2.", "0.3.")):
            raise RuntimeError("This test requires HiveMind 0.2 or 0.3")
        password = args.password_file.read_text().strip() if args.password_file else subprocess.check_output(ssh_options + [args.ssh_host,
            "cat ~/hivemind/.state/owner-password.txt"], stderr=subprocess.DEVNULL, timeout=30, **hidden).decode().strip()
        try:
            api("POST", "/api/login", {"password": password})
        finally:
            password = None
        if api("GET", "/api/providers").get("provider") not in {"hermes", "hermes-account"}:
            raise RuntimeError("This live test requires the configured Hermes account")
        print("PASS: Owner authentication and configured subscription selected", flush=True)
        stage = "pairing"
        code = api("POST", "/api/pairing", {})["code"]
        device = TestDevice(base)
        paired = device.call("POST", "/api/nodes/pair", {
            "code": code, "name": marker, "kind": "pc", "capabilities": CAPABILITIES, "harnesses": []})
        node_id, device.token = paired["node_id"], paired["token"]
        report["node_id"] = node_id
        journal = Journal(run_dir / "journal.sqlite3")
        worker = Node(device, Folder(shared), journal, lambda _: None)
        worker.heartbeat()
        if args.folder_grant:
            scope = worker.manifest()['scope']
            api('POST', '/api/nodes/'+node_id+'/permissions', {
                'capabilities': CAPABILITIES, 'approval_level': 'folder', 'scope_id': scope['id']})
            report['approval_level'] = 'folder'
            print('Granted access to the isolated test folder once.', flush=True)
        stage = "live multi-step planning"
        prompt = (f"Isolated connection test {marker}. Use only the selected test computer. "
                  f"First use device.info to check its shared-folder scope. Then list path '.' to check "
                  f"that {FILENAME} is absent. Create the NEW file {FILENAME} in that shared folder with "
                  f"exactly {CONTENT} (no extra newline); never overwrite an existing file. "
                  f"Read {FILENAME} back to verify its exact contents. Finish with a short result. "
                  "Use only device.info, files.list, files.write, and files.read. No cloud subtask is needed.")
        parent = api("POST", "/api/tasks", {"prompt": prompt, "node_id": node_id})
        parent_id = parent["id"]
        known_ids.add(parent_id)
        report["parent_id"] = parent_id
        deadline, last_heartbeat = time.monotonic() + args.timeout, 0
        checked_pending = False
        while time.monotonic() < deadline:
            if time.monotonic() - last_heartbeat >= 10:
                worker.heartbeat()
                last_heartbeat = time.monotonic()
            tasks = api("GET", "/api/state")["tasks"]
            parent = next(t for t in tasks if t["id"] == parent_id)
            children = sorted((t for t in tasks if t.get("parent_id") == parent_id), key=lambda t: t["step_no"])
            known_ids.update(t["id"] for t in children)
            completed = [t for t in children if t["status"] == "completed"]
            if any(t["status"] in TERMINAL - {"completed"} for t in children):
                raise AssertionError("A workflow step failed or was cancelled")
            if parent["status"] in TERMINAL:
                break
            for action in children:
                if action["status"] == "awaiting_approval":
                    check_action(action, node_id, parent_id, completed)
                    if args.folder_grant:
                        raise AssertionError('Routine action did not use the saved folder grant')
                    if not checked_pending:
                        assert device.call("GET", "/api/node/work")["task"] is None
                        checked_pending = True
                        print("PASS: unapproved step was not dispatched", flush=True)
                    if action["capability"] == "files.write":
                        assert not (shared / FILENAME).exists()
                    device.approved[action["id"]] = {key: action.get(key) for key in
                                                       ("node_id", "parent_id", "capability", "arguments")}
                    api("POST", f"/api/tasks/{action['id']}/approve", {})
                    worker.tick()
                    print(f"Executed approved step {action['step_no']}: {action['capability']}", flush=True)
                elif action["status"] in {"queued", "waiting_device"}:
                    if args.folder_grant:
                        check_action(action, node_id, parent_id, completed)
                        assert action.get('approval') == 'grant'
                        assert action.get('approval_scope_id') == worker.manifest()['scope']['id']
                        device.approved[action['id']] = {key: action.get(key) for key in
                            ('node_id', 'parent_id', 'capability', 'arguments')}
                    worker.tick()
                elif action.get("capability") not in CAPABILITIES and action["status"] != "planning":
                    raise ValueError("Unexpected capability in the test workflow")
            time.sleep(1)
        else:
            raise TimeoutError("The multi-step workflow did not finish")
        stage = "result verification"
        assert parent["status"] == "completed" and parent["is_workflow"]
        assert 4 <= len(children) <= 8 and all(t["status"] == "completed" for t in children)
        assert [t["step_no"] for t in children] == list(range(1, len(children) + 1))
        prior = []
        for action in children:
            check_action(action, node_id, parent_id, prior)
            result = action["result"]
            if action["capability"] == "device.info":
                assert result["shared_folder"] == shared.name
            elif action["capability"] == "files.list" and not any(t["capability"] == "files.write" for t in prior):
                assert not any(entry["name"] == FILENAME for entry in result["entries"])
            elif action["capability"] == "files.write":
                assert result["created"] is True and result["path"] == FILENAME
                assert result["bytes_written"] == len(CONTENT.encode())
            elif action["capability"] == "files.read":
                assert result["path"] == FILENAME and result["content"] == CONTENT
            prior.append(action)
        assert all(cap in [t["capability"] for t in children] for cap in CAPABILITIES)
        assert (shared / FILENAME).read_bytes() == CONTENT.encode()
        if args.folder_grant:
            assert all(t.get('approval') == 'grant' for t in children)
            print('PASS: all steps completed under one folder grant; no per-step approvals.', flush=True)
        assert isinstance(parent["result"]["text"], str) and parent["result"]["text"].strip()
        report["steps"] = [{"id": t["id"], "step": t["step_no"], "capability": t["capability"], "status": t["status"]} for t in children]
        report["passed"] = True
        print("PASS: real Hermes planned, executed, and finished the scope/list/create/read workflow", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        report["failure"] = {"stage": stage, "type": error.__class__.__name__}
        print(f"FAIL: {stage} ({error.__class__.__name__})", flush=True)
    finally:
        if parent_id:
            try:
                api("POST", f"/api/tasks/{parent_id}/cancel", {})
            except Exception:
                report["cancel_cleanup_failed"] = True
        if node_id:
            try:
                api("DELETE", "/api/nodes/" + node_id)
                try:
                    device.call("POST", "/api/node/heartbeat", {})
                except HTTPError as error:
                    assert error.code == 401
                else:
                    raise AssertionError("Revoked node was accepted")
                report["revoked"] = True
                print("PASS: temporary node revoked and its token rejected", flush=True)
            except Exception as error:
                report["revocation_failure"] = error.__class__.__name__
                report["passed"] = False
        if parent_id:
            try:
                payload = {"marker": marker, "parent_id": parent_id, "node_id": node_id,
                           "observed_task_ids": sorted(known_ids)}
                result = subprocess.run(ssh_options + [args.ssh_host,
                    "~/hivemind/.venv/bin/python -c " + shlex.quote(ARCHIVE)],
                    input=json.dumps(payload), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=30, check=True, **hidden)
                archived = json.loads(result.stdout)
                report.update(archived)
                print(f"PASS: archived only {len(archived['task_ids'])} test tasks and {len(archived['message_ids'])} linked messages", flush=True)
            except Exception as error:
                report["archive_failure"] = error.__class__.__name__
                report["passed"] = False
                print("CLEANUP INCOMPLETE: exact test IDs are in the local report; no broad history cleanup was attempted", flush=True)
        if journal:
            journal.db.close()
        try:
            api("POST", "/api/logout", {})
        except Exception:
            pass
        ssh.terminate()
        try:
            ssh.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ssh.kill()
            ssh.wait(timeout=5)
        report.setdefault("task_ids", sorted(known_ids))
        (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Report: " + str(run_dir / "report.json"), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
