"""Regression coverage for work waiting on occupied model slots."""
import asyncio
import json
import time

from fastapi.testclient import TestClient
import pytest

import hivemind.app as app_module
from test_workflows import pair, wait_until


class NeverCalledProvider:
    def __init__(self):
        self.calls = 0

    async def reply(self, messages, system):
        self.calls += 1
        raise AssertionError("The occupied model slots should prevent this call")

    def status(self):
        return {"provider": "test"}


class BusyModelRuntime:
    """Keep all model slots occupied and shorten only the app's model deadline."""
    def __init__(self):
        self.slots = asyncio.Semaphore(0)

    def Semaphore(self, count):
        return self.slots

    async def wait_for(self, awaitable, timeout):
        return await asyncio.wait_for(awaitable, min(timeout, 0.05))

    def __getattr__(self, name):
        return getattr(asyncio, name)


def test_model_queue_wait_has_a_deadline_and_does_not_call_provider(tmp_path, monkeypatch):
    # Before the fix, wait_for surrounded only model.reply, after semaphore
    # acquisition. A queued task therefore stayed in planning indefinitely.
    monkeypatch.setattr(app_module, "asyncio", BusyModelRuntime())
    provider = NeverCalledProvider()
    app = app_module.create_app(tmp_path, provider)
    with TestClient(app) as client:
        client.post("/api/login", json={"password": app.state.password_file.read_text()})
        response = client.post("/api/tasks", json={"prompt": "Summarize this request"})
        assert response.status_code == 200
        task_id = response.json()["id"]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            task = app.state.store.task(task_id)
            if task["status"] == "failed":
                break
            time.sleep(0.01)
        assert task["status"] == "failed", "A queued model request had no deadline"
        assert task["error"]
        assert provider.calls == 0
        assert client.get("/api/state").status_code == 200


def test_cancel_while_waiting_for_model_capacity_remains_cancelled(tmp_path, monkeypatch):
    runtime = BusyModelRuntime()
    monkeypatch.setattr(app_module, "asyncio", runtime)
    provider = NeverCalledProvider()
    app = app_module.create_app(tmp_path, provider)
    with TestClient(app) as client:
        client.post("/api/login", json={"password": app.state.password_file.read_text()})
        task_id = client.post("/api/tasks", json={"prompt": "Wait for model capacity"}).json()["id"]
        assert client.post(f"/api/tasks/{task_id}/cancel").json()["status"] == "cancelled"
        time.sleep(0.08)
        assert app.state.store.task(task_id)["status"] == "cancelled"
        assert provider.calls == 0


class FastLeaseRuntime:
    async def sleep(self, delay):
        await asyncio.sleep(min(delay, 0.02))

    def __getattr__(self, name):
        return getattr(asyncio, name)


@pytest.fixture
def connected(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "asyncio", FastLeaseRuntime())
    app = app_module.create_app(tmp_path, NeverCalledProvider())
    with TestClient(app) as client:
        client.post("/api/login", json={"password": app.state.password_file.read_text()})
        device, auth = pair(client)
        yield app, client, device, auth


def claim_info(app, client, device, auth):
    response = client.post("/api/tasks", json={"prompt": "Read device scope", "node_id": device["node_id"],
                           "capability": "device.info", "arguments": {}})
    task_id = response.json()["id"]
    wait_until(lambda: app.state.store.task(task_id)["status"] == "awaiting_approval")
    assert client.post(f"/api/tasks/{task_id}/approve").status_code == 200
    claimed = client.get("/api/node/work", headers=auth).json()["task"]
    assert claimed["id"] == task_id
    return claimed


@pytest.mark.parametrize("reported", ["idle", "other_task", "stale_lease"])
def test_idle_or_mismatched_heartbeat_does_not_extend_claim(connected, reported):
    app, client, device, auth = connected
    claimed = claim_info(app, client, device, auth)
    original_deadline = time.time() + 8
    app.state.store.execute("UPDATE tasks SET lease_until=? WHERE id=?", (original_deadline, claimed["id"]))
    payload = {}
    if reported != "idle":
        payload["active_task"] = {"id": "another-task" if reported == "other_task" else claimed["id"],
                                  "lease_id": "stale-lease" if reported == "stale_lease" else claimed["lease_id"]}
    response = client.post("/api/node/heartbeat", headers=auth, json=payload)
    assert response.status_code == 200
    assert app.state.store.task(claimed["id"])["lease_until"] == original_deadline


def test_matching_active_claim_can_renew_but_an_expired_claim_cannot(connected):
    app, client, device, auth = connected
    claimed = claim_info(app, client, device, auth)
    app.state.store.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() + 8, claimed["id"]))
    payload = {"active_task": {"id": claimed["id"], "lease_id": claimed["lease_id"]}}
    assert client.post("/api/node/heartbeat", headers=auth, json=payload).status_code == 200
    assert app.state.store.task(claimed["id"])["lease_until"] >= time.time() + 100
    expired_deadline = time.time() - 1
    app.state.store.execute("UPDATE tasks SET lease_until=? WHERE id=?", (expired_deadline, claimed["id"]))
    assert client.post("/api/node/heartbeat", headers=auth, json=payload).status_code == 200
    wait_until(lambda: app.state.store.task(claimed["id"])["status"] == "outcome_unknown")
    assert app.state.store.task(claimed["id"])["lease_until"] == expired_deadline


def test_cancel_deadline_is_bounded_and_heartbeats_cannot_postpone_it(connected):
    app, client, device, auth = connected
    claimed = claim_info(app, client, device, auth)
    before_cancel = time.time()
    response = client.post(f"/api/tasks/{claimed['id']}/cancel")
    assert response.json()["status"] == "cancel_requested"
    cancelled_deadline = app.state.store.task(claimed["id"])["lease_until"]
    assert cancelled_deadline <= before_cancel + 31
    payload = {"active_task": {"id": claimed["id"], "lease_id": claimed["lease_id"]}}
    response = client.post("/api/node/heartbeat", headers=auth, json=payload)
    assert claimed["id"] in response.json()["cancelled_task_ids"]
    assert app.state.store.task(claimed["id"])["lease_until"] == cancelled_deadline
    app.state.store.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, claimed["id"]))
    wait_until(lambda: app.state.store.task(claimed["id"])["status"] == "outcome_unknown")


def test_chat_receives_current_offline_then_revoked_inventory_despite_stale_history(tmp_path):
    class CaptureProvider:
        def __init__(self):
            self.system = ""
            self.messages = []

        async def reply(self, messages, system):
            self.system, self.messages = system, messages
            return json.dumps({"reply": "Connection status checked.", "task": None})

        def status(self):
            return {"provider": "test"}

    provider = CaptureProvider()
    app = app_module.create_app(tmp_path, provider)
    with TestClient(app) as client:
        client.post("/api/login", json={"password": app.state.password_file.read_text()})
        device, _ = pair(client)
        app.state.store.message("assistant", "Your test computer is connected and available.")
        app.state.store.execute("UPDATE nodes SET last_seen=? WHERE id=?", (time.time() - 100, device["node_id"]))
        assert client.post("/api/chat", json={"message": "Is my computer connected?", "mode": "chat"}).status_code == 200
        marker = "CURRENT DEVICE INVENTORY (authoritative now): "
        inventory, _ = json.JSONDecoder().raw_decode(provider.system.split(marker, 1)[1])
        assert len(inventory) == 1 and inventory[0]["online"] is False
        assert inventory[0]["id"] == device["node_id"]
        assert client.delete("/api/nodes/" + device["node_id"]).status_code == 200
        assert client.post("/api/chat", json={"message": "Do you still have access?", "mode": "chat"}).status_code == 200
        inventory, _ = json.JSONDecoder().raw_decode(provider.system.split(marker, 1)[1])
        assert inventory == []
        assert "Past messages, memories, and task results do not establish current access" in provider.system
        assert any("connected and available" in message["content"] for message in provider.messages)
