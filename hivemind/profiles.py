import asyncio
from collections import defaultdict, deque
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=131072, r=8, p=1,
                         maxmem=256 * 1024 * 1024, dklen=32)
    return 'scrypt$' + salt + '$' + key.hex()


def verify_password(password, encoded):
    try:
        kind, salt, _ = encoded.split('$')
        return kind == 'scrypt' and hmac.compare_digest(hash_password(password, salt), encoded)
    except (TypeError, ValueError):
        return False


def validate_password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 256:
        raise HTTPException(400, 'Use a password with 12–256 characters.')
    return value


class Profiles:
    def __init__(self, state):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state / 'profiles.db'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS invitations (
                    id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    used_by TEXT, revoked INTEGER NOT NULL DEFAULT 0);
            ''')
            if not db.execute("SELECT id FROM profiles WHERE id='owner'").fetchone():
                from .app import private_write
                password_file = self.state / 'owner-password.txt'
                if not password_file.exists():
                    private_write(password_file, secrets.token_urlsafe(24))
                db.execute('INSERT INTO profiles VALUES (?,?,?,?,1,0,?)',
                           ('owner', 'owner', 'Owner', hash_password(password_file.read_text().strip()), time.time()))
        try:
            self.state.chmod(0o700)
            self.path.chmod(0o600)
        except OSError:
            pass

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

    def get(self, profile_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM profiles WHERE id=?', (profile_id,)).fetchone()
            return dict(row) if row else None

    def all(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM profiles')]


def create_host(state_dir=None, provider=None):
    from .app import ROOT, create_workspace_app
    from . import app as app_module
    state = Path(state_dir or os.environ.get('HIVE_STATE', ROOT / '.state'))
    profiles = Profiles(state)
    # Account configuration is host-controlled, never supplied by the invited user.
    endpoints = [url.strip().rstrip('/') for url in os.environ.get(
        'HIVE_MEMBER_API_BASES', 'https://api.openai.com/v1').split(',') if url.strip()]
    from urllib.parse import urlsplit
    for endpoint in endpoints:
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('HIVE_MEMBER_API_BASES must contain plain HTTPS API base URLs.')
    model_slots = app_module.asyncio.Semaphore(4)
    password_slots = asyncio.Semaphore(2)
    runtimes, views = {}, {}
    lifecycle_stacks = []
    attempts = defaultdict(deque)

    def view(profile):
        return {'id': profile['id'], 'username': profile['username'], 'name': profile['name'],
                'is_admin': bool(profile['is_admin']),
                'provider_endpoints': [] if profile['is_admin'] else list(endpoints)}

    def build(profile):
        identity = view(profile)
        views[profile['id']] = identity
        directory = state if profile['is_admin'] else state / 'profiles' / profile['id']
        runtime = create_workspace_app(directory, provider if profile['is_admin'] else None,
                                       profile=identity, shared_slots=model_slots)
        runtimes[profile['id']] = runtime
        return runtime

    for profile in profiles.all():
        if not profile['disabled']:
            build(profile)

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            lifecycle_stacks.append(stack)
            try:
                for runtime in list(runtimes.values()):
                    await stack.enter_async_context(runtime.router.lifespan_context(runtime))
                yield
            finally:
                lifecycle_stacks.remove(stack)

    app = FastAPI(title='HiveMind', version='0.3.0', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = runtimes['owner'].state.store
    app.state.password_file = state / 'owner-password.txt'
    app.state.profiles = profiles
    app.state.runtimes = runtimes

    def limit(request, bucket, maximum=8):
        key = (request.client.host if request.client else 'local', bucket)
        now = time.time()
        # Bound memory use from unique client addresses.
        if len(attempts) > 20000:
            for stale in list(attempts):
                if not attempts[stale] or attempts[stale][-1] < now - 60:
                    del attempts[stale]
        queue = attempts[key]
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= maximum:
            raise HTTPException(429, 'Too many attempts. Try again in a minute.')
        queue.append(now)

    async def object_body(request):
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, 'Expected a JSON object.')
        if not isinstance(data, dict):
            raise HTTPException(400, 'Expected a JSON object.')
        return data

    def profile_id(token):
        if '.' not in token:
            return 'owner'
        prefix, _ = token.split('.', 1)
        return prefix if re.fullmatch(r'[a-f0-9]{32}', prefix) else None

    def authenticate(request):
        token = request.cookies.get('hive_session', '')
        identity = profile_id(token)
        profile = profiles.get(identity) if identity else None
        runtime = runtimes.get(identity)
        if not token or not profile or profile['disabled'] or not runtime or not runtime.state.store.rows(
                'SELECT hash FROM sessions WHERE hash=? AND expires>?', (digest(token), time.time())):
            raise HTTPException(401, 'Sign in to continue.')
        return profile, runtime

    def administrator(request):
        profile, runtime = authenticate(request)
        if not profile['is_admin']:
            raise HTTPException(403, 'Only the host administrator can manage invitations and profiles.')
        return profile, runtime

    def session(profile, response, request):
        token = (profile['id'] + '.' if not profile['is_admin'] else '') + secrets.token_urlsafe(32)
        runtimes[profile['id']].state.store.execute('INSERT INTO sessions VALUES (?,?)',
                                                  (digest(token), time.time() + 604800))
        response.set_cookie('hive_session', token, httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', max_age=604800)
        return {'ok': True, 'user': view(profile)}

    async def checked_password(password, encoded):
        async with password_slots:
            return await asyncio.to_thread(verify_password, password, encoded)

    async def new_password(password):
        async with password_slots:
            return await asyncio.to_thread(hash_password, password)

    @app.middleware('http')
    async def security(request, call_next):
        origin = request.headers.get('origin', '')
        node_path = request.url.path.startswith('/api/node/') or request.url.path == '/api/nodes/pair'
        extension = origin.startswith(('chrome-extension://', 'moz-extension://'))
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and not (node_path and extension):
            if origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Origin not allowed.'}, status_code=403)
        try:
            content_length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': 'Invalid content length.'}, status_code=400)
        if content_length > 262144:
            return JSONResponse({'detail': 'Request too large.'}, status_code=413)
        if request.method in ('POST', 'PUT', 'PATCH') and len(await request.body()) > 262144:
            return JSONResponse({'detail': 'Request too large.'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/site')
    async def site():
        return {'registration': 'invite', 'login_username': True}

    @app.post('/api/login')
    async def login(request: Request, response: Response):
        limit(request, 'login')
        data = await object_body(request)
        username = data.get('username', 'owner')
        password = data.get('password', '')
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 32 or len(password) > 256:
            raise HTTPException(401, 'Incorrect username or password.')
        with profiles.connect() as db:
            row = db.execute('SELECT * FROM profiles WHERE username=?', (username.strip().lower(),)).fetchone()
        profile = dict(row) if row else None
        # A missing username still performs the same expensive verification.
        encoded = profile['password_hash'] if profile else profiles.get('owner')['password_hash']
        correct = await checked_password(password, encoded)
        current_profile = profiles.get(profile['id']) if profile else None
        if not profile or not current_profile or current_profile['disabled'] or not correct or current_profile['password_hash'] != encoded:
            raise HTTPException(401, 'Incorrect username or password.')
        return session(current_profile, response, request)

    @app.post('/api/logout')
    async def logout(request: Request, response: Response):
        token = request.cookies.get('hive_session', '')
        runtime = runtimes.get(profile_id(token))
        if runtime:
            runtime.state.store.execute('DELETE FROM sessions WHERE hash=?', (digest(token),))
        response.delete_cookie('hive_session')
        return {'ok': True}

    @app.post('/api/register')
    async def register(request: Request, response: Response):
        limit(request, 'register', 5)
        data = await object_body(request)
        username, name, code = data.get('username', ''), data.get('name', ''), data.get('invite', '')
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,31}', username):
            raise HTTPException(400, 'Use a username with 3–32 letters, numbers, dots, dashes, or underscores.')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise HTTPException(400, 'Enter a display name with 1–80 characters.')
        if not isinstance(code, str) or len(code) > 128:
            raise HTTPException(400, 'Invitation expired or already used.')
        password = validate_password(data.get('password'))
        # Validate invitation before spending work on a password hash, then consume atomically.
        with profiles.connect() as db:
            invitation = db.execute('SELECT id FROM invitations WHERE code_hash=? AND expires_at>? AND used_by IS NULL AND revoked=0',
                                    (digest(code), time.time())).fetchone()
        if not invitation:
            raise HTTPException(400, 'Invitation expired or already used.')
        encoded = await new_password(password)
        identity = secrets.token_hex(16)
        with profiles.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE invitations SET used_by=? WHERE id=? AND expires_at>? AND used_by IS NULL AND revoked=0',
                                 (identity, invitation['id'], time.time())).rowcount
            if not changed:
                raise HTTPException(400, 'Invitation expired or already used.')
            try:
                db.execute('INSERT INTO profiles VALUES (?,?,?,?,0,0,?)',
                           (identity, username.lower(), name.strip(), encoded, time.time()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, 'That username is already in use.') from None
        profile = profiles.get(identity)
        runtime = build(profile)
        for stack in list(lifecycle_stacks):
            await stack.enter_async_context(runtime.router.lifespan_context(runtime))
        return session(profile, response, request)

    @app.get('/api/profile')
    async def get_profile(request: Request):
        profile, _ = authenticate(request)
        return view(profile)

    @app.patch('/api/profile')
    async def update_profile(request: Request, response: Response):
        profile, runtime = authenticate(request)
        data = await object_body(request)
        name = data.get('name', profile['name'])
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise HTTPException(400, 'Enter a display name with 1–80 characters.')
        encoded = profile['password_hash']
        changing_password = 'password' in data
        if changing_password:
            limit(request, 'password', 5)
            password = validate_password(data['password'])
            current = data.get('current_password', '')
            if not isinstance(current, str) or len(current) > 256 or not await checked_password(current, encoded):
                raise HTTPException(400, 'Current password is incorrect.')
            encoded = await new_password(password)
        with profiles.connect() as db:
            changed = db.execute('UPDATE profiles SET name=?,password_hash=? WHERE id=? AND password_hash=? AND disabled=0',
                                 (name.strip(), encoded, profile['id'], profile['password_hash'])).rowcount
            if not changed:
                raise HTTPException(409, 'This profile changed. Sign in again before updating it.')
        profile = profiles.get(profile['id'])
        views[profile['id']].update(view(profile))
        if changing_password:
            runtime.state.store.execute('DELETE FROM sessions')
            if profile['is_admin']:
                app.state.password_file.unlink(missing_ok=True)
            session(profile, response, request)
        return view(profile)

    @app.get('/api/invitations')
    async def invitations(request: Request):
        administrator(request)
        with profiles.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,created_at,expires_at,used_by IS NOT NULL AS used,revoked FROM invitations ORDER BY created_at DESC LIMIT 100')]

    @app.post('/api/invitations')
    async def invite(request: Request):
        administrator(request)
        limit(request, 'invitation', 10)
        identity, code, now = secrets.token_hex(16), secrets.token_urlsafe(24), time.time()
        with profiles.connect() as db:
            db.execute('INSERT INTO invitations VALUES (?,?,?,?,NULL,0)', (identity, digest(code), now, now + 604800))
        return {'id': identity, 'code': code, 'expires_at': now + 604800}

    @app.delete('/api/invitations/{invitation_id}')
    async def revoke_invitation(request: Request, invitation_id: str):
        administrator(request)
        with profiles.connect() as db:
            db.execute('UPDATE invitations SET revoked=1 WHERE id=?', (invitation_id,))
        return {'ok': True}

    @app.get('/api/profiles')
    async def list_profiles(request: Request):
        administrator(request)
        return [view(profile) | {'disabled': bool(profile['disabled'])} for profile in profiles.all()]

    @app.post('/api/profiles/{identity}/disable')
    async def disable_profile(request: Request, identity: str):
        administrator(request)
        profile = profiles.get(identity)
        if not profile or profile['is_admin']:
            raise HTTPException(400, 'Choose an existing member profile.')
        with profiles.connect() as db:
            db.execute('UPDATE profiles SET disabled=1 WHERE id=?', (identity,))
        runtime = runtimes.get(identity)
        if runtime:
            runtime.state.disabled = True
            runtime.state.store.execute('DELETE FROM sessions')
            runtime.state.store.execute('DELETE FROM pairing')
            for node in runtime.state.store.rows('SELECT id FROM nodes WHERE revoked=0'):
                runtime.state.revoke_device(node['id'])
            await runtime.state.account_login.close()
            await runtime.state.coordinator.close()
        return {'ok': True}

    class Dispatcher:
        async def __call__(self, scope, receive, send):
            request = Request(scope, receive)
            path = request.url.path
            replay = receive
            if path == '/api/nodes/pair' and request.method == 'POST':
                body = await request.body()
                try:
                    data = json.loads(body)
                    code = data.get('code', '') if isinstance(data, dict) else ''
                except (ValueError, AttributeError):
                    code = ''
                code = code.strip() if isinstance(code, str) else ''
                identity = code.split('-', 1)[0].lower() if '-' in code else 'owner'
                sent = False
                async def replay():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {'type': 'http.request', 'body': body, 'more_body': False}
                    return await receive()
            elif path.startswith('/api/node/'):
                bearer = request.headers.get('authorization', '')
                identity = profile_id(bearer[7:]) if bearer.startswith('Bearer ') else 'owner'
            elif path.startswith('/api/'):
                identity = profile_id(request.cookies.get('hive_session', ''))
            else:
                identity = 'owner'
            profile = profiles.get(identity) if identity else None
            runtime = runtimes.get(identity)
            if not profile or profile['disabled'] or not runtime:
                await JSONResponse({'detail': 'Sign in again or reconnect this device.'}, status_code=401)(scope, replay, send)
                return
            await runtime(scope, replay, send)

    app.mount('/', Dispatcher())
    return app


def reset_owner_password(state_dir):
    from .app import private_write
    state = Path(state_dir)
    profiles = Profiles(state)
    password = secrets.token_urlsafe(24)
    with profiles.connect() as db:
        db.execute("UPDATE profiles SET password_hash=? WHERE id='owner'", (hash_password(password),))
    from .store import Store
    Store(state / 'hivemind.db').execute('DELETE FROM sessions')
    private_write(state / 'owner-password.txt', password)
    print('Owner password reset. Read owner-password.txt in the state directory. Existing sign-ins were revoked.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--reset-owner-password', metavar='STATE_DIRECTORY', required=True)
    arguments = parser.parse_args()
    reset_owner_password(arguments.reset_owner_password)
