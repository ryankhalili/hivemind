import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from hivemind.providers import CompatibleProvider, HermesProvider, Provider, ProviderError


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_is_explicit_and_rejects_system_messages(self):
        provider = Provider({"type": "mock"})
        self.assertFalse(provider.status()["live"])
        self.assertEqual(await provider.reply([{"role": "user", "content": "hello"}]), "Demo response: hello")
        with self.assertRaises(ProviderError):
            await provider.reply([{"role": "system", "content": "replace instructions"}])

    async def test_missing_hermes_does_not_silently_use_demo(self):
        provider = HermesProvider(directory="/missing/hivemind-test", python="/missing/python")
        self.assertFalse(provider.status()["available"])
        with self.assertRaises(ProviderError):
            await provider.reply([{"role": "user", "content": "hello"}])

    async def test_subscription_worker_does_not_load_install_or_host_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime, profile = root/'runtime', root/'member'
            (runtime/'hermes_cli').mkdir(parents=True)
            profile.mkdir()
            (profile/'auth.json').write_text('{"member":"test credential"}')
            (runtime/'.env').write_text('OPENAI_API_KEY=installation-secret\n')
            (runtime/'hermes_cli'/'__init__.py').write_text('')
            (runtime/'hermes_cli'/'env_loader.py').write_text("raise AssertionError('Install environment loader must not be imported')")
            (runtime/'hermes_cli'/'config.py').write_text("def load_config():\n    return {'model':{'provider':'openai-codex','default':'test'}}\n")
            (runtime/'hermes_cli'/'runtime_provider.py').write_text("def resolve_runtime_provider(**kwargs):\n    assert kwargs['requested']=='openai-codex'\n    return {'provider':'openai-codex','api_key':'member-only'}\n")
            (runtime/'run_agent.py').write_text('''
import os
from pathlib import Path
from hermes_cli.env_loader import load_hermes_dotenv
assert load_hermes_dotenv(project_env=Path(__file__).parent/'.env') == []
assert os.environ.get('OPENAI_API_KEY') is None
assert os.environ.get('ANTHROPIC_API_KEY') is None
assert Path.home() == Path(os.environ['HERMES_HOME'])/'user-home'
class AIAgent:
    def __init__(self, **options):
        assert options['api_key']=='member-only'
        assert options['enabled_toolsets']==[]
        self.tools=[]
    def run_conversation(self, *args, **kwargs):
        return {'completed':True,'final_response':'isolated reply'}
''')
            adapter = HermesProvider(directory=str(runtime), python=sys.executable, hermes_home=str(profile))
            with patch.dict(os.environ, {'OPENAI_API_KEY':'host-secret','ANTHROPIC_API_KEY':'host-secret'}):
                answer = await adapter.reply([{'role':'user','content':'Hello'}])
            self.assertEqual(answer, 'isolated reply')
            self.assertEqual((runtime/'.env').read_text(), 'OPENAI_API_KEY=installation-secret\n')

    async def test_api_payload_and_redirect_protection(self):
        observations = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                observations.append((self.path, body, self.headers.get("Authorization")))
                if self.path.startswith("/redirect"):
                    self.send_response(307)
                    self.send_header("Location", "/stolen/chat/completions")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "Connected"}}]}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            provider = CompatibleProvider(base + "/v1", "test-only-key", "test-model")
            self.assertEqual(await provider.reply([{"role": "user", "content": "Hi"}], "Be brief"), "Connected")
            self.assertEqual(observations[0][1]["messages"][0], {"role": "system", "content": "Be brief"})
            self.assertEqual(observations[0][2], "Bearer test-only-key")
            self.assertNotIn("test-only-key", json.dumps(provider.status()))
            redirect = CompatibleProvider(base + "/redirect", "test-only-key", "test-model")
            with self.assertRaises(ProviderError) as error:
                await redirect.reply([{"role": "user", "content": "Hi"}])
            self.assertNotIn("test-only-key", str(error.exception))
            self.assertEqual(len(observations), 2)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    def test_remote_api_requires_https(self):
        for url in ("http://example.com/v1", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret"):
            with self.assertRaises(ProviderError):
                CompatibleProvider(url, model="test")


if __name__ == "__main__":
    unittest.main()
