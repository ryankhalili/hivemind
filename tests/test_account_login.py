import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hivemind.account_login import AccountLogin, AccountLoginError, valid_model


FAKE_HERMES = '''
import os,json
from pathlib import Path
from hermes_cli.env_loader import load_hermes_dotenv

assert load_hermes_dotenv(project_env=Path(__file__).parent.parent / '.env') == []
assert os.environ.get('PYTHON_DOTENV_DISABLED') == '1'
assert Path.home() == Path(os.environ['HERMES_HOME']) / 'user-home'

async def _start_device_code_flow(provider):
    assert provider == "openai-codex"
    assert Path(os.environ["CODEX_HOME"]).parent == Path(os.environ["HERMES_HOME"])
    return {"session_id":"internal-id", "verification_url": "VERIFICATION_URL",
            "user_code":"ABCD-EFGH", "expires_in":900, "access_token":"must-never-leak"}

async def poll_oauth_session(provider, session_id):
    if "MODE" == "success":
        (Path(os.environ["HERMES_HOME"])/"auth.json").write_text(json.dumps({"test":"fake-credential"}))
        return {"status":"approved", "access_token":"must-never-leak"}
    if "MODE" == "error":
        return {"status":"error", "error_message":"must-never-leak"}
    return {"status":"pending", "access_token":"must-never-leak"}
'''


class AccountLoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        (self.runtime / 'hermes_cli').mkdir(parents=True)
        (self.runtime / 'hermes_cli' / '__init__.py').write_text('')
        self.state = self.root / 'state'
        self.state.mkdir()
        self.manager = None

    async def asyncTearDown(self):
        if self.manager:
            await self.manager.close()
        self.temp.cleanup()

    def manager_for(self, mode, url='https://auth.openai.com/codex/device'):
        source = FAKE_HERMES.replace('MODE', mode).replace('VERIFICATION_URL', url)
        (self.runtime / 'hermes_cli' / 'web_server.py').write_text(source)
        self.manager = AccountLogin(self.state, hermes_root=str(self.runtime), python=sys.executable)
        return self.manager

    async def terminal(self, manager):
        for _ in range(100):
            if manager.status()['status'] not in {'pending', 'starting'}:
                return manager.status()
            await asyncio.sleep(0.02)
        self.fail('Login did not terminate')

    async def test_success_uses_isolated_profile_and_sanitized_events(self):
        manager = self.manager_for('success')
        original = self.root / 'existing-hermes'
        original.mkdir()
        (original / 'auth.json').write_text('unchanged-original-auth')
        with patch.dict(os.environ, {'HERMES_HOME': str(original)}):
            first = await manager.start('gpt-test-model')
            status = await self.terminal(manager)
        self.assertNotIn('must-never-leak', json.dumps(first))
        self.assertEqual(status['status'], 'connected')
        self.assertEqual(status['model'], 'gpt-test-model')
        self.assertNotIn('must-never-leak', json.dumps(status))
        self.assertEqual(manager.profile_dir.parent, self.state / 'hermes-profile')
        self.assertTrue((manager.profile_dir / 'auth.json').exists())
        self.assertEqual((original / 'auth.json').read_text(), 'unchanged-original-auth')
        restored = AccountLogin(self.state, hermes_root=str(self.runtime), python=sys.executable)
        self.assertEqual(restored.status()['status'], 'connected')
        self.assertEqual(restored.profile_dir, manager.profile_dir)

    async def test_cancel_stops_worker_without_saving_auth(self):
        manager = self.manager_for('pending')
        status = await manager.start('gpt-test')
        self.assertEqual(status['status'], 'pending')
        self.assertEqual(status['verification_url'], 'https://auth.openai.com/codex/device')
        process = manager._process
        repeated = await manager.start('gpt-test')
        self.assertEqual(repeated['user_code'], status['user_code'])
        self.assertIs(process, manager._process)
        self.assertEqual((await manager.cancel())['status'], 'cancelled')
        self.assertIsNotNone(process.returncode)
        self.assertFalse(manager._active_file.exists())

    async def test_unexpected_verification_host_is_rejected(self):
        manager = self.manager_for('pending', 'https://phishing.example/login')
        await manager.start('gpt-test')
        status = await self.terminal(manager)
        self.assertEqual(status['status'], 'error')
        self.assertNotIn('phishing', json.dumps(status))
        self.assertFalse(manager._active_file.exists())

    async def test_upstream_error_details_never_reach_ui(self):
        manager = self.manager_for('error')
        await manager.start('gpt-test')
        status = await self.terminal(manager)
        self.assertEqual(status['status'], 'error')
        self.assertNotIn('must-never-leak', json.dumps(status))

    def test_invalid_model_names_rejected(self):
        for model in ('', '../foo', 'gpt-test; shell', ['model'], 'm' * 121):
            with self.assertRaises(AccountLoginError):
                valid_model(model)


if __name__ == '__main__':
    unittest.main()
