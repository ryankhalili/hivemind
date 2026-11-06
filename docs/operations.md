# Hosting HiveMind

Use this guide to run a server for yourself or a small invited group. HiveMind runs on Python 3.11 or later. The server needs persistent storage and outbound access to the model providers you choose. Device tools run on connected devices, not on the server.

## 1. Install and start

Place the repository at `~/hivemind` on a Linux host, under a dedicated unprivileged account:

```sh
cd ~/hivemind
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/python -m hivemind
```

By default the app listens on `127.0.0.1:8787`. For first setup, either use the browser on that machine or an SSH forward:

```sh
ssh -L 8787:127.0.0.1:8787 your-server
```

Open `http://127.0.0.1:8787` on your computer. Sign in as `owner` with the generated `.state/owner-password.txt`, then change the password in Settings → Your profile. That file is a bootstrap credential, not a password to distribute to members. Set your display name there too.

State is private to the host account. The application uses separate databases and settings for each profile, but the server administrator remains trusted. Keep `.state` out of web-server document roots, Git, and public backups.

## 2. Connect a model

In Settings → AI connection, choose an API endpoint and model or use subscription sign-in. A compatible endpoint must implement `/chat/completions`. API credentials stay in the profile's encrypted settings on the server.

API connections need no external agent installation. Subscription sign-in and existing-account reuse require an installed Hermes runtime. For a nonstandard installation, set these in the service environment file:

```ini
HIVE_HERMES_ROOT=/absolute/path/to/hermes-agent
HIVE_HERMES_PYTHON=/absolute/path/to/hermes-agent/venv/bin/python
```

The owner can reuse an account already configured on the host. Members must connect their own model account; they cannot select the owner's host authentication. Members' API destinations are restricted to the administrator's allowlist, defaulting to `https://api.openai.com/v1`:

```ini
HIVE_MEMBER_API_BASES=https://api.openai.com/v1,https://your-provider.example/v1
```

Only list providers you intend the server to contact. Members cannot use this form to reach arbitrary internal services or localhost. A local model endpoint is available to the host owner. Configure the host environment deliberately: it can contain credentials used by the owner's existing-account adapter.

`HIVE_PROVIDER=mock` enables labeled development replies for the owner. It is useful for UI checks, not real task planning. New members start without a model connection.

## 3. Keep it running

The supplied user service assumes the checkout is `~/hivemind`. Adjust `WorkingDirectory` and `ExecStart` if it is elsewhere.

```sh
mkdir -p ~/.config/systemd/user ~/.config/hivemind
cp scripts/hivemind.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hivemind
systemctl --user status hivemind
```

Optional settings go in `~/.config/hivemind/environment`. Restrict that file to the service account. Enable lingering for that account if the service must run after logout:

```sh
sudo loginctl enable-linger "$USER"
journalctl --user -u hivemind -n 50
```

Run **one application worker**. Coordinator jobs, login attempts, and profile runtimes are kept in that process. Multiple workers or replicas need a shared job scheduler and are not supported by this version.

## 4. Give it a stable HTTPS address

Point a domain you control at the server. Allow inbound TCP 80 and 443 through the host and hosting-provider firewalls. Keep port 8787 bound to loopback.

Install Caddy using its [official instructions](https://caddyserver.com/docs/install), replace `hivemind.example.com` in `scripts/Caddyfile` with your domain, and use that site block in your Caddy configuration. Caddy handles the public HTTPS connection and proxies to `127.0.0.1:8787`. Follow the [HTTPS quick-start](https://caddyserver.com/docs/quick-starts/https) for certificate prerequisites and service setup.

The launcher trusts forwarded headers only from the loopback proxy. This lets secure cookies and same-origin checks see the original HTTPS scheme. If you change the proxy topology, configure the trusted proxy addresses explicitly; do not expose the application directly with unrestricted forwarded-header trust. See [FastAPI's proxy guidance](https://fastapi.tiangolo.com/advanced/behind-a-proxy/).

A named tunnel with a domain is another option. For short development sessions, the optional `scripts/install_tunnel.py --arch arm64` or `--arch amd64` downloads a verified `cloudflared` binary. `scripts/hivemind-tunnel.service` runs a temporary tunnel. [Quick Tunnel addresses can change and have no uptime guarantee](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/); use a stable address before depending on a shared installation.

## 5. Invite people and connect devices

Create an invitation in Profile and give the recipient the server address plus that code. Each recipient signs up with their own username and password, connects a model, and pairs their own devices. Codes expire and can only be used once. Signup does not grant access to the owner's data or model account.

Pairing codes are different from invitations: create a pairing code in Devices, then enter it into a connector. A code expires after ten minutes. The connector authenticates with its own revocable credential afterward.

In Devices, choose which capabilities are available and whether routine reads or new-file creation may run under a standing folder grant. Default behavior asks every time. Enabling commands is a separate local choice; folder access alone never enables command execution.

## 6. Build downloads

On Windows, install PyInstaller into a build environment and run:

```sh
python -m pip install pyinstaller
python scripts/build-installers.py
```

This creates a Windows installer, Mac source launcher, and Chrome extension ZIP in `downloads/`. On macOS/Linux the script builds the ZIPs; build the Windows executable on Windows. Copy reviewed builds to the server's `downloads/` directory. The dashboard serves them at `/downloads/`.

Windows builds are currently unsigned. The Mac launcher requires Python with Tk. These are early distribution packages; there is no automatic update service. Preserve dependency license notices when distributing builds.

## Updates, backup, and restore

Before changing a deployed installation:

1. Stop `hivemind` so no account can write while you copy state.
2. Back up **all** of `.state` and record the current source revision. Include the profile registry, each profile directory, encryption keys, model sign-in profiles, and device/session databases. Store the copy privately, preferably outside the application directory.
3. Update source and dependencies without replacing `.state` with a development copy.
4. Restart `hivemind`, inspect `/health`, and run the deployment check below.
5. Test restoration on a separate directory and port before relying on the backup.

The 0.3 upgrade preserves the existing owner in the root state directory and creates a registry for additional accounts. Per-profile schema changes are additive. Rollback means stopping the app and restoring matching source **and** the complete pre-update state; do not run old code against a partially upgraded directory.

```sh
python scripts/check_deployment.py https://hivemind.example.com \
  --username owner --password-file /private/path/password.txt --downloads
```

The password file is read without printing its contents. Omit `--downloads` before building connector packages.

## Recovery

If the administrator loses their password, stop the service and use the local recovery command:

```sh
python -m hivemind.profiles --reset-owner-password /absolute/path/to/.state
```

It creates a new bootstrap password file and invalidates the owner's sessions. Start the service, sign in, and change the password. Recovery requires access to the host; there is no email password-reset service.

A lost device should be disconnected in Devices. Desktop **Disconnect** revokes its server credential, then removes that credential locally. If offline, the connector stops work and keeps a pending revocation to retry. Closing the window only pauses it and preserves pairing.

## Task recovery and logs

Queued work and approval requests survive restarts. Running actions become `outcome_unknown`; check the device before repeating them. Planning interrupted by restart fails. Parent tasks waiting for a completed child are reconciled after startup.

Stopping requests cancellation; it cannot undo completed effects. The current device claim is shortened to at most 30 seconds. A lost acknowledgment leaves an unknown outcome. **Try again** creates a new task and, for uncertain outcomes, requires the user to confirm they checked the device.

Use task events for action history and the service journal for application errors. Do not add passwords, bearer tokens, API keys, or complete provider responses to routine logs.

`scripts/smoke_remote.py --ssh-host your-server` runs a paid live-model workflow against an installation at `~/hivemind`. It creates an isolated temporary connector and folder, checks approval and file operations, revokes the connector, and archives only its own test records. It reads the remote bootstrap password unless `--password-file` is supplied. Add `--folder-grant` to verify the same workflow under one standing folder permission, without per-step approval. Use it deliberately, not as a startup probe.
