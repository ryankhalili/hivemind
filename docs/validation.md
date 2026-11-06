# Release checks

Run the automated checks from the repository root:

```sh
python -m pytest -q
node connectors/browser/test.mjs
node --check web/app.js
```

The suite uses isolated temporary state and synthetic model responses. It covers accounts and invitations, profile separation, device ownership, task lifecycle, folder grants, exact approvals, cancellation, reconnect, memory, provider configuration, and local connector boundaries.

The CI configuration runs these checks on Linux and Windows with Python 3.11 and 3.13. A locally passing run is not evidence that those remote jobs have executed; GitHub runs them after the repository is published.

## Version 0.3 verification — November 6, 2026

- Windows automated suite: 113 passed, one symbolic-link privilege skip. Browser connector and dashboard regression checks passed.
- Isolated Linux startup: owner sign-in, invited signup, separate data and model settings, device ownership, and disabled-account rejection passed.
- Browser: invitation signup, empty member device list, model onboarding, folder-grant save, same-tab invitation handling, and all three guide diagrams checked.
- Live model workflow: device information, folder listing, new-file creation, and read-back completed under one standing folder grant. No per-step approvals were sent. Temporary device access was revoked afterward.
- Public HTTPS sign-in, origin rejection, docs, and connector downloads passed; the published Windows binary matched the reviewed local build.

These results do not replace the hardware and integration limits below.

## Before distributing a build

1. Start a clean server and complete owner setup.
2. Create an invitation, register a second account, and verify its chats, devices, memory, and model connection start empty.
3. Verify one account cannot read or act on another account's task, device, memory, or events, including when it knows an ID.
4. Pair a temporary desktop folder. Check ask, read, and read-plus-create levels; sensitive actions must still wait for approval.
5. Narrow or remove a grant before a queued action is claimed. The old approval must not survive.
6. Change the shared folder and verify the old grant is invalid. Verify an attempted overwrite leaves the old file untouched.
7. Exercise local confirmation, optional HiveMind approval, cancellation, offline disconnect, and reconnect with a new pairing code.
8. Build connector packages, check hashes, and inspect the archived file list for private state or credentials.
9. View signup, permissions, task review, and the architecture guide in a browser at desktop and narrow widths.
10. Run the deployment health/authentication check after updating the server.

Use only temporary folders and synthetic accounts for tests that create files or profiles. Real-model tests are separate because they use the configured account and may cost money.

## Hardware and integration limits

The development path has live Windows-to-server file workflow coverage and browser UI checks. Mac source packaging can be inspected without proving a Mac installation works. Native Android/iOS execution is not implemented. A new subscription account's complete provider-hosted authorization requires a consenting account owner; mocked helper tests do not substitute for that.

The Windows symbolic-link test is skipped when the test account lacks permission to create symbolic links. Other path and junction checks remain separate. Installers are unsigned; there is no automatic update channel. A source release is not a signed consumer release.
