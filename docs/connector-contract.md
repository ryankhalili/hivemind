# Connector contract v0.3

All paths are relative to the HiveMind server. Requests and responses use JSON. Production connections require HTTPS. Every authenticated API request belongs to one profile. Device credentials are separate from browser sessions and are revocable. Routing prefixes in a member credential identify its profile but do not authorize access.

## Profile session and conversation

- `POST /api/login {username,password}` establishes an HttpOnly session cookie.
- `POST /api/logout {}` invalidates the current profile session.
- `GET /api/state` returns `{messages:[],tasks:[],nodes:[],memories:[],memory_stats:{},provider:{},user:{id,username,name,is_admin,provider_endpoints}}`.
- `POST /api/chat {message,mode:"auto"|"chat"|"task",node_id?:string}` returns `{message,task?}`. The dashboard uses `auto`. Background work continues independently of later conversation.
- Messages include `id,role,content,created_at,kind,task_id`. A final task reply can carry `task_id` so the dashboard links back to its source workflow. Display ordinary conversation in chat; `kind:"activity"` and `kind:"system_test"` are not assistant conversation turns. Archived records are omitted from normal state responses and memory recall.

## Tasks and steps

`POST /api/tasks {prompt,node_id?:string,capability?:string,arguments?:object}` creates a task. A model-planned task is a parent workflow; supplying an explicit capability creates a concrete action.

Task records include `id,parent_id,is_workflow,step_no,prompt,status,node_id,capability,arguments,result,error,approval,approval_scope_id,created_at,updated_at`. State responses also include `last_progress:{message,created_at}` when a device has reported progress. Parent display should reflect its active child, including required approval; do not show only a generic working label. A parent has a stable ID and up to eight sequential child steps. Each child has its own ID, action, approval, events, and result. Independent parent tasks can progress concurrently. A node accepts one active action at a time.

States: `planning`, `waiting_children`, `awaiting_approval`, `queued`, `waiting_device`, `running`, `cancel_requested`, `completed`, `failed`, `cancelled`, `outcome_unknown`.

- `POST /api/tasks/{id}/approve {}` approves the displayed immutable child action. A parent cannot be approved as an open-ended grant.
- `POST /api/tasks/{id}/cancel {}` requests a stop. Stopping a parent also stops further steps and requests cancellation of an active child.
- `POST /api/tasks/{id}/retry {}` creates a new task ID for a failed or cancelled root task. For `outcome_unknown`, the owner must check the device and send `{confirmed_outcome:true}`. Child steps cannot be retried directly. Never retry automatically.
- `GET /api/tasks/{id}/events` returns timeline records with `id,task_id,kind,detail,created_at`.

Approved work waits in `waiting_device` while its node is offline. A running stop request stays `cancel_requested` until a valid result arrives or its claim expires. `cancelled` is an acknowledgment, not rollback of prior effects. A device that finished first may still report success. Cancellation shortens a running claim to at most 30 more seconds; heartbeats cannot extend that stopping deadline. Loss of confirmation becomes `outcome_unknown`.

## Pairing and permissions

- `POST /api/pairing {}` requires the owner session and returns `{code,expires_at}`. The code expires after ten minutes and works once.
- `POST /api/nodes/pair {code,name,kind,capabilities:[...],harnesses?:[{id,name,description,revision?}],scope?:{id,name},local_confirmation?:boolean}` returns `{node_id,token}`. Supported kind labels are `pc`, `mac`, `browser`, `android`, `ios`, and `cloud`. A label does not imply an implementation exists for that platform.
- All subsequent node calls use `Authorization: Bearer <token>`. The server derives identity from the token, never from a caller-supplied node ID.
- State node records expose `id,name,kind,capabilities,offered_capabilities,manifest,last_seen,online,scope,approval_level,approved_scope_id,local_confirmation`. `capabilities` are owner-enabled; `offered_capabilities` are the node's advertisement. `manifest.harnesses` lists registered local agents.
- `POST /api/nodes/{id}/permissions {capabilities:[...],approval_level?:"ask"|"read"|"folder",scope_id?:string}` requires the device owner's profile session. Choose only offered capabilities. For a read/folder grant, send the current `scope.id` as `scope_id`; stale scope saves return 409.
- `DELETE /api/nodes/{id}` revokes the node immediately.
- `POST /api/node/disconnect {}` lets a node revoke itself before deleting its local credential. Desktop Disconnect must complete this operation; Close only pauses the app. If revocation cannot reach the server, persist pending revocation and block reconnect until it succeeds.

Pairing enables offered basic capabilities but excludes `command.run` and `harness.run`. These require explicit review. Changing the offered capabilities or agent manifest disables both again, invalidates affected pending work, and requests cancellation of affected running work. Removing an enabled permission cancels pending affected actions and requests a stop for running ones. Re-enabling it does not revive those actions. Sensitive actions always require explicit approval. Routine folder actions can use the standing grant described below.

Desktop agent registrations include a 64-character SHA-256 `revision` covering their frozen executable configuration, executable bytes, and explicitly configured script files. The connector checks it again before launching. Changed code requires reconnecting and renewed server permission review. This is change detection for a trusted local connector, not remote attestation or a sandbox for an untrusted operating-system account.

## Standing folder grants

`scope.id` is a 64-character lowercase SHA-256 identity for the locally selected folder; `scope.name` is a short display label, not a trusted path. The desktop derives identity from the canonical path and filesystem directory identity. The server never selects a replacement local folder. Omitting scope preserves compatibility but prevents a standing grant.

`ask` requires exact approval for every action. `read` permits enabled `device.info`, `files.list`, and `files.read`; `folder` also permits enabled `files.write`, which only creates new files. Arguments must be bounded and folder-relative. Browser actions, commands, and registered harnesses always wait for exact approval.

Task `approval` is `pending`, `explicit`, or `grant`; `approval_scope_id` binds a granted action to a scope. `approved_by_grant` events explain automatic authorization. Dispatch rechecks the current grant. Reducing a grant returns affected queued actions to review and requests cancellation of active granted work. Changing scope resets the level to `ask` and cancels pending device work. A new grant does not revive cancelled tasks.

`local_confirmation` reports a device-local preference. True is the default: commands and agents require a local prompt after dashboard approval. False permits exact dashboard approval to suffice, only after the device user enabled that option for this server. The server permissions endpoint cannot change it. The connector still checks local capability enablement, agent registration, frozen code, and `approval:"explicit"` before executing.

## Claiming, progress, and results

`POST /api/node/heartbeat {}` refreshes availability. A connector can instead send `{capabilities:[...],harnesses:[...],scope:{id,name},local_confirmation:boolean}` to refresh its advertised manifest. The response is `{ok:true,cancelled_task_ids:[]}`; check those IDs while executing. To renew active work, include `active_task:{id,lease_id}` for the action actually being executed. An ordinary availability heartbeat does not extend task claims. Only a matching, unexpired claim in `running` is extended.

`GET /api/node/work` returns `{task:null}` or `{task:{...taskFields,lease_id}}`. Claiming is atomic and initially lasts 120 seconds. The task is bound to this node and claim ID.

`POST /api/node/tasks/{id}/progress {lease_id,message}` adds a bounded progress event. It requires the current claim.

`POST /api/node/tasks/{id}/result {lease_id,status:"completed"|"failed"|"cancelled",result?:object,error?:string}` reports the actual outcome. The server accepts a current claim in `running` or `cancel_requested`; it rejects expired, duplicate, foreign, and finalized claims. A rejected report does not authorize repeating the action.

Persist the task ID before executing. Persist the result before sending it. Re-send a stored result if appropriate, but do not repeat a consequential action after a lost response. Check stop requests during long work. A connector that stops a process should report what it knows, including partial effects, rather than assuming nothing happened.

## Capabilities

| Capability | Arguments / meaning |
|---|---|
| `device.info` | Device description. |
| `files.list` | `{path}`, relative to the shared folder. |
| `files.read` | `{path}`, bounded text from the shared folder. |
| `files.write` | `{path,content}`, a new text file; no overwrite. |
| `browser.open` | `{url}`, open a permitted URL. |
| `browser.read` | Read an explicitly shared browser tab. |
| `cloud.answer` | `{prompt}`, model writing/reasoning with no server computer tools. |
| `command.run` | `{argv:[program,...args],cwd:".",timeout:60}`, optional local program execution. |
| `harness.run` | `{harness_id,goal,cwd:".",timeout:60,context:{}}`, an agent registered locally by ID. |

Command/agent timeouts are bounded at 300 seconds. The node chooses registered-agent executable configuration locally; the model cannot register a new executable. The local user must enable execution and choose whether exact approval can come from HiveMind alone or must also be confirmed locally. This is not an operating-system sandbox: a permitted program may access resources outside its starting folder.

Reject unknown capabilities and unsupported arguments. Bound file and result sizes. Basic file operations must reject path escapes and symbolic links. Browser nodes must limit reading to explicitly shared tabs. Native mobile system control is not implemented; phones currently use the web client. A future connector must obey its platform's execution and permission rules.

## Memory

- `GET /api/memories` returns saved notes.
- `POST /api/memories {content}` saves a note.
- `DELETE /api/memories/{id}` removes it and conservatively resets archived and older working context. Visible chat stays.
- `GET /api/memory/candidates` returns pending suggestions: `{id,content,source_ids:[...],evidence:[{message_id,quote}],created_at}`.
- `POST /api/memory/candidates/{id}/accept {}` creates an ordinary saved note.
- `POST /api/memory/candidates/{id}/dismiss {}` records rejection without saving the note.

Consolidation runs in the background after chat. Older message batches become summaries, summaries become broader rollups, and retrieval can return original source excerpts. Working and recalled context are bounded; this is not infinite memory.

## Model connections

`GET /api/providers` returns safe metadata, including `display_name` when available. Never return credentials.

`POST /api/providers {type:"hermes"|"hermes-account"|"compatible",base_url?,model?,api_key?}` selects the model connection. The dashboard names the subscription service, while `hermes` remains an internal adapter identifier.

Subscription sign-in uses:
- `POST /api/providers/login/start {model?}`
- `GET /api/providers/login/status`
- `POST /api/providers/login/cancel {}`

Status values include `idle`, `starting`, `pending`, `connected`, `expired`, `error`, and `cancelled`. Pending status can include `verification_url,user_code,expires_at`; connected status includes `profile_available`. Display a safe HTTPS verification link only after an owner explicitly starts sign-in. After authorization, `POST /api/providers {type:"hermes-account",model?}` activates the isolated server-owned profile. Do not accept credential paths from a client or expose tokens to the browser.

Every endpoint in this section requires the current profile session. Members cannot select `type:"hermes"` (host credentials); their compatible endpoints must be on the host-configured `HIVE_MEMBER_API_BASES` list. A new profile starts unconfigured. Missing member API keys never inherit a host environment key. Starting sign-in, pairing a device, and signing into the workspace are three different operations.

## Profiles and invitations

- `GET /api/site` returns `{registration:"invite",login_username:true}` without authentication.
- `POST /api/register {invite,username,name,password}` consumes a valid invitation and creates a profile session. Usernames use 3–32 letters, numbers, dots, dashes, or underscores and begin with a letter or number. They are case-insensitive. Passwords require 12–256 characters.
- `GET /api/profile` returns the signed-in profile's safe metadata.
- `PATCH /api/profile {name?}` updates the display name. Include `{current_password,password}` to change the password, invalidate old sessions, and receive a fresh current session.
- Administrator only: `POST /api/invitations {}` creates `{id,code,expires_at}`. Its secret code is shown only once. An invitation expires after seven days and works once.
- Administrator only: `GET /api/invitations` returns status metadata; `DELETE /api/invitations/{id}` revokes one.
- Administrator only: `GET /api/profiles` lists names, usernames, roles and disabled state; `POST /api/profiles/{id}/disable` disables a member, clears sessions and pairing codes, revokes devices, and stops work. It retains their history on disk. The administrator cannot disable the owner through this endpoint.

A member pairing code includes its profile ID and a random one-use secret. A member session or node token includes a profile prefix and a random secret. Treat the entire value as opaque and preserve its case. Never derive authorization from the prefix. Old owner credentials without a prefix remain supported.

Invitations should be carried in a URL fragment (`/#join=...`), not a query string that enters server access logs. Do not put device tokens, model tokens, or passwords in URLs.
