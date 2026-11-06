# Subteam handoffs

These assignments follow the working skeleton rather than the earlier spreadsheet task numbers. Each subteam should own a small next increment that another group can use. Give each increment one lead, a reproducible demonstration, and a clear acceptance check.

## 1. Memory & Context

**Own:** `hivemind/memory.py`; new memory-specific tests. Coordinate changes to persistent records with Infrastructure.

**Starting point:** explicit notes and reviewed suggestions, keyword retrieval, bounded recent conversation, and a two-level archive of summaries linked to source messages. Candidate suggestions remain separate from accepted notes. There is no embedding-based semantic search yet.

**First increment:** build a small evaluation set from synthetic or voluntarily contributed histories. Include preferences, changed dates, ambiguous references, irrelevant notes, and deleted notes. Compare the existing keyword method with one alternative. Keep the same bounded return format so Chat does not need to change for each experiment.

**Acceptance:** publish per-case retrieval results; report false matches and missed matches; show a measurable improvement on held-out cases; preserve memory and source IDs; test recall after several summary levels. Deletion currently resets archived and older working context while keeping visible history. Evaluate a more selective deletion method only if it reliably prevents paraphrased facts from returning.

**Work with:** Testing on independent examples; Cybersecurity on consent and deletion; Models on context limits.

## 2. Agent Planning & Coordination

**Own:** `hivemind/coordinator.py` and its interface with `chat()` in `hivemind/app.py`.

**Starting point:** a conversation call creates a stable parent task. The coordinator plans up to eight sequential child steps and uses earlier results to choose the next action. Routine folder actions can use a standing grant; sensitive actions require exact approval. Independent workflows can run concurrently, with one active action per device.

**First increment:** evaluate the implemented read-and-draft path and add independent outcome checks for it. Then design a small branching plan with two independent reads and a final draft. Make dependencies visible and preserve the current sequential behavior.

**Acceptance:** step two cannot start before a successful step-one report; a failed read stops the dependent step; cancellation prevents new dispatch; no result is treated as permission; the user can keep chatting while work runs. Restart behavior must be specified and tested before deployment.

**Work with:** Security on approval boundaries; Infrastructure on schema migration; Testing on lost and duplicate messages.

## 3. Models & Local AI

**Own:** `hivemind/providers.py` and provider-specific tests.

**Starting point:** isolated Hermes model calls using existing authentication, plus an OpenAI-compatible HTTP adapter. The model adapter has no device tools. Conversation, planning, and cloud response use the same configured provider.

**First increment:** allow a separate model choice for conversation and planning. Add a locally served model as a tested configuration. Measure response latency, valid-plan rate, task correctness, and resource use against the existing setup.

**Acceptance:** changing a model does not change connector contracts or permission rules; an invalid plan fails clearly; quota/authentication failures do not leak credentials; local-model requirements are documented; results identify tested hardware and model versions. Do not label a hosted model as local merely because the dashboard runs locally.

**Work with:** Memory on bounded context; Product on account setup; Security on credential storage and authorized provider flows.

## 4. Tools & Device Integration

**Own:** `connectors/desktop/`, `connectors/browser/`, and capability implementations. Coordinate shared message changes through `docs/connector-contract.md`.

**Starting point:** scoped file operations and explicitly enabled command/registered-agent execution on desktop; shared-tab reading and opening in Chrome. Both connectors journal task IDs before execution. Device manifests advertise local agents; privileged capabilities need local and server consent. Neither connector is a complete general computer-use agent.

**First increment:** add a structured browser excerpt capability that returns the shared page's headings and selected article text with source URL and title. Define its output shape and text limits. Keep the same explicit tab-sharing rule. Keep this increment narrow and independently testable.

**Acceptance:** unsupported arguments and out-of-scope targets fail locally; the exact action is visible before approval; duplicate delivery never repeats the action; closing/reopening the connector preserves its connection and execution journal; cancellation and reconnect behavior are demonstrated.

**Later:** background browser service/native messaging, additional registered-agent adapters, Android/iOS native capabilities, and platform-specific permission prompts. Define each platform's actual permitted actions before implementing a universal “run anything” tool.

**Work with:** Security on local scope; Testing on path escapes and replay; Infrastructure on installers and updates.

## 5. Infrastructure & Deployment

**Own:** `hivemind/store.py`, `hivemind/profiles.py`, startup/deployment configuration, `scripts/build-installers.py`, database migrations, backup and restore, and shared transport conventions.

**Starting point:** one FastAPI process with SQLite; outbound polling connectors; short model-call concurrency limits; durable task records and explicit restart outcomes. Deployment configuration should remain separate from model and tool behavior.

**First increment:** add a documented backup/restore procedure and a versioned database migration mechanism. Validate a clean deployment on a second test machine using only the repository and its documented setup.

**Acceptance:** restore preserves task history, saved notes, and device ownership; secrets are transferred separately and are not included in public artifacts; running tasks after restart become uncertain rather than silently re-executing; a failed deployment can be rolled back without losing the prior database.

**Packaging increment:** produce a signed Windows release and a native Mac package with an update/uninstall path. The current Windows build has optional start-at-sign-in and reconnect controls, but is unsigned; the Mac launcher needs Python/Tk; make those limits visible until replacement packages exist.

**Work with:** all teams on compatibility; Security on server hardening and key rotation; Product on understandable installation.

## 6. Cybersecurity & Privacy

**Own:** security design and tests around owner sessions, pairing, permissions, secrets, untrusted content, and data boundaries. Implement changes with the module's primary owner.

**Starting point:** invited profiles with separate state and credentials; expiring sessions and pairing codes; revocable device tokens; standing folder grants and exact sensitive-action approval; local file/tab restrictions; explicit command/agent permissions; model adapters with no direct tools. Changed manifests revoke privileged execution until reviewed again. The server and local operating-system accounts remain trusted.

**First increment:** write a short threat model and attack the existing boundaries. Test a foreign device result, replayed approval, expired pairing code, revoked token, path traversal, malicious note, and a webpage that instructs the model to exfiltrate data. Separate failures in model judgment from failures in server enforcement.

**Acceptance:** unauthorized actions are rejected even if the model proposes them; no credential appears in API responses or normal logs; important tests run automatically; each finding has a concrete reproduction and owner. Extend cross-profile and model-credential isolation tests whenever a new API or provider is added.

**Work with:** Memory on deletion across derived data; Tools on local enforcement; Product on approvals a person can understand.

## 7. Testing & Evaluation

**Own:** `tests/`, reproducible end-to-end scenarios, baseline results, and release acceptance checks. Feature owners still write their own component tests.

**Starting point:** tests exercise server and connector behavior, with mock model calls where appropriate. A connector's success report is accepted as completion; independent objective verification remains open.

**First increment:** create three repeatable workflows covering cloud conversation, approved shared-file reading, and approved browser-tab reading. Add failures at the boundaries: disconnect after claiming, lose the result acknowledgment, restart the server, revoke a device, and cancel while running.

**Acceptance:** expected state and actual state match in each case; failures cannot be counted as successes; model-free checks run without API cost; live evaluations report the provider, version/configuration, number of repetitions, and observed success rate. Keep evaluation cases separate from examples used to tune planning prompts.

**Work with:** Planning on objective verification; Memory on retrieval evaluation; Product on whether the tasks helped actual users.

## 8. User Experience & User Research

**Own:** `web/`, setup language, the user journey, usability studies, and coordination of desktop installer screens with Tools and Infrastructure.

**Starting point:** invitation signup, profile sign-in, chat, task review, pairing, folder-grant controls, memories, and account settings. Phones use the web dashboard. A subscription sign-in wizard invokes the installed Hermes authorization helper and keeps credentials in a dedicated server profile; that new-login path has automated coverage but has not been completed with a new live account during this build. Invited members now have their own profiles, model connections, and devices.

**First increment:** observe several new users connecting a computer, choosing a shared folder, approving a file read, and finding its result. Record where they hesitate or require help. Improve the flow without hiding what a device is allowed to access.

**Acceptance:** a new user can complete the flow using the application and its short instructions; the approval screen names the action, device, and affected data; errors tell the person what to do next; keyboard and mobile layouts remain usable. Measure completion, assistance needed, and time spent rather than assuming the flow is intuitive.

**Account setup increment:** test the subscription authorization journey with a consenting account owner, improve expired-code and cancellation recovery, and extend the provider picker only through documented provider flows. Show whether the AI account lives on the server or device. A successful UI animation is not proof that an account is authenticated.

## Shared working agreement

- Each task has one primary owner and named collaborators.
- Agree on a small demonstration before implementation. A short issue should state the input, expected result, affected interface, and failure case.
- Discuss changes to `connector-contract.md` before changing both sides independently. Add a protocol version when compatibility starts to diverge.
- Keep credentials, connection files, state databases, and user examples out of commits and downloadable installers.
- Give experiments a baseline and report unsuccessful results. Do not replace working behavior solely because an approach is newer.
- Keep documentation of implemented behavior separate from proposals. A diagram should say when a component is planned.

The shared milestone is a repeatable journey: **sign in on a phone, ask for a text file on a paired computer and a draft based on it, approve the read, follow the next step under the same task ID, and keep chatting while work runs.** Then exercise cancellation and reconnect during a registered local-agent task. This gives every subteam a concrete system to improve.
