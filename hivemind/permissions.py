import json
import re
from pathlib import PurePosixPath


LEVELS = {'ask', 'read', 'folder'}
READ_ACTIONS = {'device.info', 'files.list', 'files.read'}
FOLDER_ACTIONS = READ_ACTIONS | {'files.write'}


def clean_scope(value):
    if value in (None, {}):
        return {}
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not re.fullmatch(r'[a-f0-9]{64}', value['id']):
        raise ValueError('Invalid shared folder identity.')
    name = value.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 160 or any(ord(c) < 32 for c in name):
        raise ValueError('Invalid shared folder name.')
    return {'id': value['id'], 'name': name}


def scope_of(device):
    value = device.get('scope') or {}
    return json.loads(value) if isinstance(value, str) else value


def grant_scope(device, capability, arguments):
    scope = scope_of(device)
    level = device.get('approval_level', 'ask')
    scope_id = scope.get('id', '')
    if not scope_id or device.get('approved_scope_id') != scope_id or level not in {'read', 'folder'}:
        return ''
    caps = device.get('capabilities', [])
    caps = json.loads(caps) if isinstance(caps, str) else caps
    if capability not in caps or capability not in (READ_ACTIONS if level == 'read' else FOLDER_ACTIONS):
        return ''
    if not isinstance(arguments, dict):
        return ''
    if capability == 'device.info':
        return scope_id if not arguments else ''
    allowed_keys = {'path', 'content'} if capability == 'files.write' else {'path'}
    if not set(arguments) <= allowed_keys:
        return ''
    path = arguments.get('path', '.')
    if not isinstance(path, str) or len(path) > 1024 or '\x00' in path or '\\' in path or ':' in path:
        return ''
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or '..' in parsed.parts:
        return ''
    if capability == 'files.write':
        content = arguments.get('content')
        if path in ('', '.') or not isinstance(content, str) or len(content.encode('utf-8')) > 65536:
            return ''
    return scope_id


def record_grant(store, task_id, scope_id, level):
    store.execute("UPDATE tasks SET approval='grant',approval_scope_id=? WHERE id=?", (scope_id, task_id))
    store.event(task_id, 'approved_by_grant', 'Allowed by saved ' + ('read access' if level == 'read' else 'folder access') + '.')


def set_approval_level(store, coordinator, device, level):
    if level not in LEVELS:
        raise ValueError('Choose ask, read, or folder access.')
    scope_id = scope_of(device).get('id', '')
    if level != 'ask' and not scope_id:
        raise ValueError('Update this connector before granting continuing folder access.')
    store.execute('UPDATE nodes SET approval_level=?,approved_scope_id=? WHERE id=?',
                  (level, scope_id if level != 'ask' else '', device['id']))
    current = dict(device, approval_level=level, approved_scope_id=scope_id if level != 'ask' else '')
    tasks = store.rows("SELECT id FROM tasks WHERE node_id=? AND is_workflow=0 AND status IN ('awaiting_approval','queued','waiting_device','running')", (device['id'],))
    for row in tasks:
        task = store.task(row['id'])
        allowed = grant_scope(current, task['capability'], task['arguments'])
        if task['approval'] == 'grant' and not allowed:
            if task['status'] == 'running':
                coordinator.cancel(task['id'])
            else:
                store.execute("UPDATE tasks SET approval='pending',approval_scope_id='' WHERE id=?", (task['id'],))
                store.update_task(task['id'], 'awaiting_approval')
                store.event(task['id'], 'approval_required', 'Saved access changed. Review this action.')
        elif task['status'] == 'awaiting_approval' and allowed:
            record_grant(store, task['id'], allowed, level)
            store.update_task(task['id'], 'queued')


def update_scope(store, coordinator, device, scope):
    scope = clean_scope(scope)
    if scope == scope_of(device):
        return
    store.execute("UPDATE nodes SET scope=?,approval_level='ask',approved_scope_id='' WHERE id=?",
                  (json.dumps(scope), device['id']))
    for row in store.rows("SELECT id,capability FROM tasks WHERE node_id=? AND is_workflow=0 AND status NOT IN ('completed','failed','cancelled','outcome_unknown')", (device['id'],)):
        coordinator.cancel(row['id'])
        store.event(row['id'], 'folder_changed', 'The shared folder changed. Start a new task after reviewing its access.')


def claim_allowed(device, task):
    if task.get('approval') != 'grant':
        return True
    scope_id = grant_scope(device, task['capability'], task.get('arguments'))
    return bool(scope_id and scope_id == task.get('approval_scope_id'))
