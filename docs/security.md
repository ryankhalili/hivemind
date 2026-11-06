# Security boundaries

HiveMind has two trusted hosts: the server holding profile data, and each device running a connector. Members are separated through the application. The server administrator and the device's operating-system user remain trusted.

## What is enforced

- Profiles use separate databases, memory, tasks, devices, sessions, and model settings. A route to a profile still requires that profile's valid credential.
- Invitations expire and are single-use. Passwords are stored as salted scrypt hashes. Browser sessions use HttpOnly, SameSite cookies and Secure cookies over HTTPS.
- Device and pairing credentials are random, stored as hashes, and revocable. A device cannot finish another device's task or submit an expired claim.
- Requests that change browser state are checked for origin. The reverse proxy must preserve the public host and trusted HTTPS scheme.
- Standing folder grants allow only defined routine capabilities. They are bound to the folder identity and checked before dispatch. A changed scope or removed permission invalidates affected work.
- Basic file tools reject traversal, absolute paths, symbolic links and junction escapes, and overwriting existing files. They enforce size limits locally.
- Commands and registered agents start disabled, need local opt-in, and require exact-action approval. Accepting approval from HiveMind instead of a local dialog is itself a local preference.
- The model proposes actions; it cannot create permissions. Tool output, webpages, memory, and prior chat remain untrusted input.
- Model workers have no device tools. Members cannot reuse host model credentials or choose arbitrary server-side API destinations. Allowed endpoints are controlled by the host administrator.
- A connector journals task IDs and results. Lost acknowledgments never authorize executing a task twice.

## What these controls do not promise

An administrator with filesystem access can read data and encryption keys. Encryption of provider settings is not whole-disk encryption or end-to-end encryption. A compromised server can misuse granted device capabilities. A malicious local operating-system process is outside the file-tool trust boundary.

Commands and custom agents are not sandboxed by their working folder. They can access the device user's other files and network resources. Review the exact action before enabling or approving these capabilities.

Read grants can send permitted file content to the configured model provider when a task needs it. They are not a promise that data stays on the computer. Browser sharing similarly allows selected page text to enter model context.

Prompt injection remains a model-behavior risk. Approval checks limit what can run, but a model may propose a harmful action or select irrelevant data within an existing grant. Use a narrow shared folder and an appropriate approval level.

This release has application tests, not an independent security audit. It has no MFA, email recovery, administrator impersonation feature, distributed rate limiter, or OS-level sandbox. Use a small invited group while those capabilities and broader platform testing develop.

## Operating safely

Use HTTPS at a stable domain, keep the app port private, update dependencies, and restrict access to state and backups. Run one worker under an unprivileged account. Do not put secrets in source files, issue reports, screenshots, or logs.

Disconnect devices that are no longer used. If a stop request has an unknown outcome, inspect the device before repeating the action. Restore matching code and private state when rolling back a version.

When reporting a suspected vulnerability, include the version and a minimal reproduction using synthetic data. Avoid posting real credentials or private file contents. If public disclosure would expose an active installation, contact its operator privately before publishing the reproduction.

## Checks to maintain

The regression suite should keep covering cross-profile reads and mutations; forged profile routing identifiers; password-change session invalidation; revoked and foreign device tokens; one-use pairing and invitations; expired claims; standing-grant revocation; changed folders; path escape; model credential isolation; explicit sensitive approvals; and disconnect during execution.

See [architecture](architecture.md) for the flow and [operations](operations.md) for hosting and recovery.
