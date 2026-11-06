"""Model adapters. Device execution stays in HiveMind, never in the model adapter."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ProviderError(RuntimeError):
    pass


def isolated_environment(home):
    allowed = {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP',
               'LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE'}
    env = {key:value for key,value in os.environ.items() if key.upper() in allowed}
    private_home = Path(home)/'user-home'
    private_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative in ('AppData/Local', 'AppData/Roaming', '.config', '.cache'):
        (private_home/relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    env.update(HERMES_HOME=str(home), CODEX_HOME=str(Path(home)/'codex'), PYTHONIOENCODING='utf-8',
               HOME=str(private_home), USERPROFILE=str(private_home),
               LOCALAPPDATA=str(private_home/'AppData'/'Local'), APPDATA=str(private_home/'AppData'/'Roaming'),
               XDG_CONFIG_HOME=str(private_home/'.config'), XDG_CACHE_HOME=str(private_home/'.cache'),
               PYTHON_DOTENV_DISABLED='1', HIVE_ISOLATED_ACCOUNT='1')
    return env


def protect_account_runtime():
    if os.environ.get('HIVE_ISOLATED_ACCOUNT') != '1':
        return
    # Hermes also reads its installation's .env. Account workers must not load it.
    import types
    loader = types.ModuleType('hermes_cli.env_loader')
    loader.load_hermes_dotenv = lambda *, hermes_home=None, project_env=None: []
    sys.modules['hermes_cli.env_loader'] = loader
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'


class _BaseProvider:
    async def reply(self, messages: list[dict], system: str = "") -> str:
        raise NotImplementedError

    def status(self) -> dict:
        raise NotImplementedError


def _messages(messages: list[dict]) -> list[dict]:
    if not messages or len(messages) > 100:
        raise ProviderError("Provide between 1 and 100 messages.")
    cleaned = []
    for message in messages:
        if message.get("role") not in {"user", "assistant"}:
            raise ProviderError("Only user and assistant messages are accepted.")
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderError("Message content must be text.")
        cleaned.append({"role": message["role"], "content": content})
    if sum(len(m["content"]) for m in cleaned) > 150_000:
        raise ProviderError("Conversation exceeds the current context allowance.")
    return cleaned


class MockProvider(_BaseProvider):
    async def reply(self, messages: list[dict], system: str = "") -> str:
        messages = _messages(messages)
        return "Demo response: " + messages[-1]["content"]

    def status(self) -> dict:
        return {"provider": "mock", "model": "deterministic", "available": True, "live": False}


class UnconfiguredProvider(_BaseProvider):
    async def reply(self, messages, system=""):
        raise ProviderError("Connect your model account in Settings first.")

    def status(self):
        return {"provider": "unconfigured", "model": "", "available": False, "live": False,
                "display_name": "No model connected"}


class HermesProvider(_BaseProvider):
    def __init__(self, directory: str | None = None, python: str | None = None,
                 model: str | None = None, timeout: float = 90, hermes_home: str | None = None):
        candidates = [
            Path.home() / ".hermes" / "hermes-agent",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes" / "hermes-agent",
        ]
        selected = directory or os.environ.get("HIVE_HERMES_ROOT") or os.environ.get("HERMES_DIRECTORY")
        self.directory = Path(selected) if selected else next((p for p in candidates if (p / "run_agent.py").is_file()), candidates[0])
        executable = self.directory / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python = str(python or os.environ.get("HIVE_HERMES_PYTHON") or os.environ.get("HERMES_PYTHON") or executable)
        self.model = model or os.environ.get("HIVE_MODEL") or os.environ.get("HIVEMIND_MODEL") or None
        self.timeout = timeout
        self.hermes_home = hermes_home

    def status(self) -> dict:
        return {"provider": "hermes-account" if self.hermes_home else "hermes", "model": self.model or "Configured model",
                "display_name": "ChatGPT / Codex subscription" if self.hermes_home else os.environ.get('HIVE_ACCOUNT_LABEL','Connected subscription'),
                "available": (self.directory / "run_agent.py").is_file() and Path(self.python).is_file(),
                "live": True, "authentication": "Subscription sign-in; checked when a request is made"}

    async def reply(self, messages: list[dict], system: str = "") -> str:
        if not self.status()["available"]:
            raise ProviderError("Hermes is not installed on this host. Connect an installed Hermes runtime or choose an API connection.")
        payload = {"messages": _messages(messages), "system": system, "model": self.model,
                   "directory": str(self.directory)}
        env = dict(os.environ)
        env.pop("HERMES_KANBAN_TASK", None)
        env["PYTHONIOENCODING"] = "utf-8"
        if self.hermes_home:
            env = isolated_environment(self.hermes_home)
        with tempfile.TemporaryDirectory(prefix="hivemind-model-") as workdir:
            process = await asyncio.create_subprocess_exec(
                self.python, str(Path(__file__).resolve()), "--hermes-worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env, cwd=workdir,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), self.timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ProviderError("The model took too long to respond. Try again.") from None
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            raise ProviderError("Hermes returned an unreadable response.") from None
        if process.returncode or not result.get("ok"):
            raise ProviderError("Hermes could not complete the request. Check its account connection and selected model.")
        return result["text"]


class CompatibleProvider(_BaseProvider):
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 90):
        self.base_url = (base_url or os.environ.get("HIVEMIND_API_BASE") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("HIVEMIND_API_KEY", "")
        self.model = model or os.environ.get("HIVEMIND_MODEL", "")
        self.timeout = timeout
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            raise ProviderError("Use a plain API base URL without credentials, query, or fragment.")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ProviderError("API connections require HTTPS, except on localhost.")

    def status(self) -> dict:
        return {"provider": "compatible", "model": self.model, "available": bool(self.model),
                "live": True, "authentication": "API key supplied" if self.api_key else "No API key supplied"}

    async def reply(self, messages: list[dict], system: str = "") -> str:
        cleaned = _messages(messages)
        if not self.model:
            raise ProviderError("Select a model for this API connection.")
        return await asyncio.to_thread(self._reply, cleaned, system)

    def _reply(self, messages: list[dict], system: str) -> str:
        body = {"model": self.model, "messages": ([{"role": "system", "content": system}] if system else []) + messages,
                "stream": False}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(), headers=headers)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None

        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(2_000_000))
            text = result["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text:
                raise ValueError("Empty output")
            return text
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("The API connection failed. Check the endpoint, account, model, and quota.") from None


def get_provider(config: dict | None = None) -> _BaseProvider:
    config = config or {}
    name = str(config.get("type") or os.environ.get("HIVE_PROVIDER") or os.environ.get("HIVEMIND_PROVIDER", "hermes")).lower()
    if name == "mock":
        return MockProvider()
    if name == "unconfigured":
        return UnconfiguredProvider()
    if name in {"compatible", "openai-compatible"}:
        return CompatibleProvider(base_url=config.get("base_url"), api_key=config.get("api_key"), model=config.get("model"), timeout=float(config.get("timeout", 90)))
    if name == "hermes-account" and not config.get("hermes_home"):
        raise ProviderError("Connect an account before selecting it.")
    if name in {"hermes", "hermes-account"}:
        return HermesProvider(model=config.get("model"), timeout=float(config.get("timeout", 90)), hermes_home=config.get("hermes_home"))
    raise ProviderError("Unknown model provider.")


class Provider:
    def __init__(self, config: dict | None = None):
        self.adapter = get_provider(config)

    async def reply(self, messages: list[dict], system: str = "") -> str:
        return await self.adapter.reply(messages, system)

    def status(self) -> dict:
        return self.adapter.status()


def _hermes_worker(payload: dict) -> str:
    sys.path.insert(0, payload["directory"])
    protect_account_runtime()
    from run_agent import AIAgent
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider

    model_config = load_config().get("model", {})
    if os.environ.get('HIVE_ISOLATED_ACCOUNT') == '1':
        if model_config.get('provider') != 'openai-codex' or not (Path(os.environ['HERMES_HOME'])/'auth.json').is_file():
            raise ProviderError('Connect your own subscription before using this account.')
    model = payload.get("model") or model_config.get("default")
    if not model:
        raise ProviderError("No Hermes model configured")
    runtime = resolve_runtime_provider(requested=model_config.get("provider"), target_model=model)
    # External CLI runtimes may execute independently of Hermes tool filtering.
    if runtime.get("command") or runtime.get("api_mode") in {"codex_app_server", "acp"}:
        raise ProviderError("Choose a model provider rather than an external agent runtime")
    with tempfile.TemporaryDirectory(prefix="hivemind-hermes-") as home:
        os.environ["HERMES_HOME"] = home
        agent = AIAgent(
            model=model, api_key=runtime.get("api_key"), base_url=runtime.get("base_url"),
            provider=runtime.get("provider"), api_mode=runtime.get("api_mode"),
            credential_pool=runtime.get("credential_pool"), enabled_toolsets=[],
            max_iterations=3, max_tokens=1800, quiet_mode=True, skip_context_files=True,
            skip_memory=True, load_soul_identity=False, session_db=None,
            save_trajectories=False, verbose_logging=False, checkpoints_enabled=False,
            session_id="hivemind-" + uuid.uuid4().hex,
        )
        if agent.tools:
            raise ProviderError("Model adapter unexpectedly enabled tools")
        messages = payload["messages"]
        if messages[-1]["role"] != "user":
            messages = messages + [{"role": "user", "content": "Continue with your response."}]
        result = agent.run_conversation(messages[-1]["content"], system_message=payload["system"], conversation_history=messages[:-1])
        if not result.get("completed") or result.get("interrupted") or not result.get("final_response"):
            raise ProviderError("Model did not complete")
        return result["final_response"]


if __name__ == "__main__" and "--hermes-worker" in sys.argv:
    payload = json.loads(sys.stdin.read())
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            output = _hermes_worker(payload)
        print(json.dumps({"ok": True, "text": output}))
    except Exception:
        print(json.dumps({"ok": False}))
        sys.exit(1)
