"""Browser sign-in backed by the installed Hermes dashboard's device-code flow."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import uuid


class AccountLoginError(RuntimeError):
    pass


def valid_model(model):
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", model):
        raise AccountLoginError("Enter a model name using letters, numbers, dots, dashes, underscores, or slashes.")
    return model


class AccountLogin:
    def __init__(self, state_dir, hermes_root=None, python=None):
        from .providers import HermesProvider
        runtime = HermesProvider(directory=hermes_root, python=python)
        self.directory, self.python = runtime.directory, runtime.python
        self.state_dir = Path(state_dir).resolve()
        self.profiles = self.state_dir / 'hermes-profile'
        self.profile_dir = self.profiles
        self._active_file = self.state_dir / 'account-profile.json'
        self._process = None
        self._reader = None
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self.closed = False
        self._view = {'status': 'idle', 'profile_available': False}
        if self._active_file.exists():
            try:
                saved = json.loads(self._active_file.read_text())
                candidate = (self.profiles / saved['profile']).resolve()
                if candidate.parent == self.profiles.resolve() and (candidate / 'auth.json').is_file():
                    self.profile_dir = candidate
                    self._view = {'status': 'connected', 'model': valid_model(saved['model']), 'profile_available': True}
            except (ValueError, KeyError, OSError, AccountLoginError):
                pass

    def status(self):
        return dict(self._view)

    async def start(self, model):
        model = valid_model(model)
        async with self._lock:
            if self.closed:
                raise AccountLoginError('This account connection is closed. Sign in to HiveMind again.')
            if self._view['status'] in {'starting', 'pending'}:
                return self.status()
            if not Path(self.python).is_file() or not (self.directory / 'hermes_cli' / 'web_server.py').is_file():
                raise AccountLoginError('This host needs a Hermes installation with dashboard account support.')
            self.profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
            attempt_dir = self.profiles / uuid.uuid4().hex
            attempt_dir.mkdir(mode=0o700)
            config = {'model': {'provider': 'openai-codex', 'default': model},
                      'auth': {'adopt_external_logins': False},
                      'memory': {'memory_enabled': False, 'user_profile_enabled': False}}
            _private_write(attempt_dir / 'config.yaml', json.dumps(config))
            from .providers import isolated_environment
            env = isolated_environment(attempt_dir)
            self._ready = asyncio.Event()
            self._view = {'status': 'starting', 'model': model, 'profile_available': self._active_file.exists()}
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self.python, str(Path(__file__).resolve()), '--login-worker', str(self.directory),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    stdin=asyncio.subprocess.DEVNULL, env=env, cwd=str(attempt_dir),
                )
            except OSError:
                self._view.update(status='error', error='Could not start account connection.')
                raise AccountLoginError(self._view['error']) from None
            self._reader = asyncio.create_task(self._read(attempt_dir, model))
        try:
            await asyncio.wait_for(self._ready.wait(), 22)
        except asyncio.TimeoutError:
            await self.cancel()
            self._view.update(status='error', error='The sign-in service did not respond. Try again.')
        return self.status()

    async def _read(self, attempt_dir, model):
        process = self._process
        try:
            while line := await asyncio.wait_for(process.stdout.readline(), timeout=930):
                if len(line) > 4096:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                status = event.get('status')
                if status == 'pending':
                    url, code = event.get('verification_url'), event.get('user_code')
                    if url != 'https://auth.openai.com/codex/device' or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9-]{3,32}', code):
                        raise AccountLoginError('Invalid sign-in response.')
                    self._view.update(status='pending', verification_url=url, user_code=code,
                                      expires_at=time.time() + min(max(int(event.get('expires_in', 900)), 1), 900))
                    self._ready.set()
                elif status == 'connected':
                    if not (attempt_dir / 'auth.json').is_file():
                        raise AccountLoginError('Account was not saved.')
                    self.profile_dir = attempt_dir
                    _private_write(self._active_file, json.dumps({'profile': attempt_dir.name, 'model': model}))
                    self._view = {'status': 'connected', 'model': model, 'profile_available': True}
                    self._ready.set()
                    break
                elif status in {'expired', 'error'}:
                    self._view = {'status': status, 'model': model, 'profile_available': self._active_file.exists(),
                                  'error': 'Sign-in expired. Try again.' if status == 'expired' else 'Account connection failed. Try again or use an API connection.'}
                    self._ready.set()
                    break
            if self._view['status'] in {'starting', 'pending'}:
                self._view.update(status='error', error='Account connection ended before sign-in completed.')
        except asyncio.CancelledError:
            raise
        except Exception:
            self._view = {'status': 'error', 'model': model, 'profile_available': self._active_file.exists(),
                          'error': 'Account connection failed. Try again.'}
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            self._ready.set()

    async def cancel(self):
        async with self._lock:
            if self._process and self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
            if self._reader and not self._reader.done():
                self._reader.cancel()
                await asyncio.gather(self._reader, return_exceptions=True)
            if self._view['status'] in {'starting', 'pending', 'error'}:
                self._view = {'status': 'cancelled', 'profile_available': self._active_file.exists()}
            self._ready.set()
            return self.status()

    async def close(self):
        self.closed = True
        await self.cancel()


def _private_write(path, content):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(content)


async def _worker(directory, emit):
    sys.path.insert(0, directory)
    if __package__:
        from .providers import protect_account_runtime
    else:
        from providers import protect_account_runtime
    protect_account_runtime()
    # Avoid Hermes's optional dependency auto-install branch during import.
    if any(importlib.util.find_spec(name) is None for name in ('fastapi', 'uvicorn', 'httpx')):
        raise AccountLoginError('Dashboard dependencies unavailable')
    from hermes_cli.web_server import _start_device_code_flow, poll_oauth_session
    started = await _start_device_code_flow('openai-codex')
    emit({'status': 'pending', 'verification_url': started['verification_url'],
          'user_code': started['user_code'], 'expires_in': started.get('expires_in', 900)})
    deadline = time.monotonic() + min(int(started.get('expires_in', 900)), 900)
    while time.monotonic() < deadline:
        result = await poll_oauth_session('openai-codex', started['session_id'])
        status = result['status']
        if status == 'approved':
            emit({'status': 'connected'})
            return
        if status in {'expired', 'error'}:
            emit({'status': status})
            return
        await asyncio.sleep(1)
    emit({'status': 'expired'})


if __name__ == '__main__' and '--login-worker' in sys.argv:
    output = sys.stdout

    def emit(event):
        output.write(json.dumps(event) + '\n')
        output.flush()

    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(_worker(sys.argv[-1], emit))
    except BaseException:
        emit({'status': 'error'})
        sys.exit(1)
