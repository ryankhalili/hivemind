# HiveMind

HiveMind connects an AI assistant to tools on your own devices. Talk from a phone or computer, let the server plan the work, and follow its progress while you keep chatting.

The device app runs the tools. It does not need a local AI model. You can also register an existing agent or your team's custom harness.

HiveMind is under active development. This release is intended for small invited groups; features and interfaces may change.

## Run locally

Requires Python 3.11 or later. From a checkout of this repository:

```sh
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[test]'
python -m hivemind
```

Open <http://127.0.0.1:8787>. Sign in as `owner` using the generated password in `.state/owner-password.txt`, then set your own password in Settings → Your profile. Choose a model connection in Settings → AI connection. An API connection works without Hermes; subscription sign-in needs an installed Hermes runtime on the server.

For an existing installation, the original owner's chats, devices, memory, and model settings remain in place. Existing device credentials continue to work.

## Share your server

[Hosting instructions](docs/operations.md) cover HTTPS, the Linux service, model setup, backups, and updates. Run one application process behind HTTPS at a stable address.

In Settings, the administrator creates an invitation. A recipient uses that invitation to create their own username, password, and profile, then connects their own model account and devices. Invitations are single-use and expire. Do not share the administrator password.

Each profile has separate chats, memory, tasks, devices, and encrypted model settings. The host administrator can still read server files; these accounts do not provide privacy from the person running the host.

## Connect a device

1. Create a pairing code in Devices.
2. Install the desktop connector, enter the server address and code, and select a folder.
3. Open the device's permissions in HiveMind and choose an approval level.

| Level | Runs without another prompt | Still asks |
|---|---|---|
| Ask every time | Nothing on a device | Every device action |
| Allow reads | Device information and reads within the granted folder | New files, browser actions, commands, agents |
| Allow reads and new files | Reads and new text files within the granted folder | Browser actions, commands, agents |

File tools never overwrite or delete existing files. A folder change removes the standing grant. You can narrow permissions or disconnect a device from the dashboard.

Commands and registered agents require local opt-in and approval of the exact action in HiveMind. They also ask on the computer unless its user enables **Accept command and agent approvals from HiveMind**. Arbitrary programs run as the computer's user; a starting folder does not confine them.

The [desktop connector](connectors/desktop/README.md) supports Windows, macOS, and Linux. The [browser connector](connectors/browser/README.md) opens URLs and reads explicitly shared tabs. Phones can use the dashboard; native Android/iOS control is not implemented.

## Code map

| Path | Responsibility |
|---|---|
| `hivemind/` | Profiles, server API, task coordination, memory, and model connections |
| `connectors/desktop/` | Desktop setup, shared-folder tools, commands, registered agents |
| `connectors/browser/` | Chrome extension and shared-tab tools |
| `web/` | Dashboard |
| `tests/` | Automated behavior and boundary checks |
| `scripts/` | Hosting, packaging, connector builds, deployment checks |
| `docs/` | Architecture, API contracts, operations, and team ownership |

Start with the [plain-language architecture](docs/architecture.md) or the illustrated guide at `/guide` after starting the server. [Subteam handoffs](docs/subteams.md) identify the parts each team can improve. Custom connectors should follow the [connector contract](docs/connector-contract.md).

## Develop and test

```sh
python -m pytest -q
node connectors/browser/test.mjs
node --check web/app.js
```

Tests use isolated state and model substitutes; they do not require paid model calls. `HIVE_PROVIDER=mock` enables labeled demo replies for local UI work. It does not emulate intelligent task planning.

```sh
# On Windows, with PyInstaller installed in the active environment
python scripts/build-installers.py
# Source ZIP and deployment archive
python scripts/package.py
```

Builds appear in `downloads/`; archives appear in the adjacent `outputs/` folder. Neither private state nor downloaded binaries belong in Git. The Windows installer is unsigned; the Mac launcher requires Python with Tk. [Release validation](docs/validation.md) records the checks and remaining hardware limits.

## Current limits

This is an early release for small invited groups. Parent tasks have up to eight sequential steps, with one active action per device. Lost acknowledgments leave an unconfirmed outcome instead of silently repeating work. A valid device report establishes completion; independent verification of every goal is not implemented.

Memory uses recent messages, summaries, source excerpts, and reviewed saved notes within a fixed budget. Recall can miss details. There is no infinite context window.

There is no native phone harness, signed auto-update system, or distributed worker deployment. The browser popup must stay open to receive work. Read the [security boundaries](docs/security.md) before enabling commands or hosting for other people.

## License

Copyright (c) 2026 Ryan Khalili and HiveMind contributors. Licensed under the [MIT License](LICENSE), which permits reuse and modification while requiring preservation of the copyright and license notice.

Hermes is an optional external runtime and is not bundled with the source. Third-party dependencies retain their own licenses. Preserve their notices when redistributing connector builds.
