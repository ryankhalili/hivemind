import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def uid():
    return uuid.uuid4().hex


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires REAL);
                CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL);
                CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, name TEXT, kind TEXT,
                    token_hash TEXT UNIQUE, capabilities TEXT, last_seen REAL, revoked INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, role TEXT, content TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, prompt TEXT, status TEXT,
                    node_id TEXT, capability TEXT, arguments TEXT, result TEXT, error TEXT,
                    lease_id TEXT, lease_until REAL, created_at REAL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, detail TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT, created_at REAL);
            ''')
            additions = {
                'tasks': {'parent_id':'TEXT', 'step_no':'INTEGER DEFAULT 0', 'is_workflow':'INTEGER DEFAULT 0', 'archived':'INTEGER DEFAULT 0', 'approval':"TEXT DEFAULT 'pending'", 'approval_scope_id':"TEXT DEFAULT ''"},
                'messages': {'kind':"TEXT DEFAULT 'chat'", 'archived':'INTEGER DEFAULT 0', 'task_id':'TEXT'},
                'nodes': {'offered_capabilities':"TEXT DEFAULT '[]'", 'manifest':"TEXT DEFAULT '{}'", 'approved_manifest_hash':"TEXT DEFAULT ''", 'scope':"TEXT DEFAULT '{}'", 'approval_level':"TEXT DEFAULT 'ask'", 'approved_scope_id':"TEXT DEFAULT ''", 'local_confirmation':'INTEGER DEFAULT 1'},
            }
            for table, columns in additions.items():
                present = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
                for name, definition in columns.items():
                    if name not in present:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            db.execute("UPDATE nodes SET offered_capabilities=capabilities WHERE offered_capabilities='[]'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, params=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params)]

    def execute(self, sql, params=()):
        with self.connect() as db:
            return db.execute(sql, params).rowcount

    def message(self, role, content, kind='chat', task_id=None):
        row = dict(id=uid(), role=role, content=content, created_at=time.time(),kind=kind,task_id=task_id)
        self.execute('INSERT INTO messages(id,role,content,created_at,kind,task_id) VALUES (:id,:role,:content,:created_at,:kind,:task_id)', row)
        return row

    def event(self, task_id, kind, detail=''):
        self.execute('INSERT INTO events(task_id,kind,detail,created_at) VALUES (?,?,?,?)',
                     (task_id, kind, detail, time.time()))

    def task(self, task_id):
        rows = self.rows('SELECT * FROM tasks WHERE id=?', (task_id,))
        return decode_task(rows[0]) if rows else None

    def update_task(self, task_id, status, **fields):
        allowed = {'node_id','capability','arguments','result','error','lease_id','lease_until'}
        assert set(fields) <= allowed
        fields.update(status=status, updated_at=time.time())
        for key in ('arguments', 'result'):
            if key in fields:
                fields[key] = json.dumps(fields[key])
        self.execute('UPDATE tasks SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',
                     (*fields.values(),task_id))
        self.event(task_id, status)


def decode_task(row):
    for key in ('arguments', 'result'):
        row[key] = json.loads(row[key]) if row[key] else None
    return row
