# Permission Handling Design — talk2me ↔ Claude Code

How a hands-free voice agent safely handles tool-permission requests when driving
`claude -p` in bidirectional stream-json. Research + design. Sources and
verification status are inline; anything I could not confirm against a primary
source is marked **[UNVERIFIED]**.

---

## 1. The problem, precisely

talk2me runs Claude Code headless as the conversation spine
(`talk2me/backends/claude_code.py`):

```
claude -p --input-format stream-json --output-format stream-json \
       --include-partial-messages --replay-user-messages --verbose \
       --session-id <uuid> --permission-mode default
```

In `--permission-mode default` there is **no auto-approval** for tools that
aren't covered by an allow rule. In an interactive TUI those land as a y/n
prompt. In headless print mode there is no TTY to prompt. The result depends on
configuration:

- With **nothing** wired for approvals, an unapproved tool in `default` mode
  effectively stalls or is denied — the turn cannot proceed and the voice loop
  has nothing useful to say. (Confirmed behavior pattern: headless `default`
  with no approver leaves the agent unable to proceed; see Sources.)
- The current backend never reads any control message and never writes anything
  to stdin except user turns, so today talk2me would silently sit through a
  permission request — the orchestrator's `_consume_turn` loop only handles
  `AssistantTextDelta`, `ToolActivity`, `TurnComplete`, `SessionReady`,
  `BackendError`, and would block on `events.get()` until `result` arrives.

So the design question is two-part:

1. **What permission posture** should a voice coding assistant run with?
2. **What mid-turn approval flow** (if the protocol supports one) lets Nate say
   "approve" / "deny" out loud and have talk2me answer the backend correctly?

---

## 2. Research findings — how permissions surface in headless stream-json

### 2.1 Permission modes (`--permission-mode`)

Verified against the Agent SDK permissions doc. Evaluation order when Claude
requests a tool: **hooks → deny rules → permission mode → allow rules →
`canUseTool` callback**.

| Mode | Behavior in headless print mode |
|---|---|
| `default` | No auto-approve. Unmatched tool falls through to `canUseTool`. With no approver wired, the call cannot be granted. |
| `acceptEdits` | Auto-approves Edit/Write + filesystem Bash (`mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp`, `sed`) **inside cwd / additionalDirectories**. Other Bash + network still fall through. |
| `plan` | Read-only tools only; Claude proposes, never edits. Can call `AskUserQuestion`. |
| `bypassPermissions` | Approves everything reaching the mode check. Deny rules + hooks still apply. Dangerous. |
| `dontAsk` | **Newer mode.** Converts any prompt into a **denial** — anything not pre-approved by `allowedTools` / settings allow-rules / a hook is denied **without** calling `canUseTool`. Hard, predictable tool surface. |

Key correctness facts (verified):

- `allowedTools` only **adds allow rules**; it does **not** constrain
  `bypassPermissions` (every tool still approved under bypass).
- `disallowedTools` with a **bare name** (`"Bash"`) removes the tool from
  Claude's context entirely; with a **scope** (`"Bash(rm *)"`) it denies
  matching calls in every mode including bypass, leaving other Bash calls to
  fall through.
- `acceptEdits` and `bypassPermissions` are **inherited by subagents** and
  cannot be overridden per-subagent — relevant because Claude Code can spawn
  Task subagents.
- For MCP tools specifically: `acceptEdits` does **not** auto-approve them;
  `bypassPermissions` does. Prefer `allowedTools: ["mcp__server__*"]` to grant
  exactly one MCP server.

### 2.2 The two distinct ways to intercept a permission request

There are **two different mechanisms**, with **different wire shapes**. This is
the crux and the place sources disagree, so it is called out explicitly.

#### Mechanism A — `--permission-prompt-tool` (an MCP tool answers)

`claude -p ... --permission-prompt-tool mcp__<server>__<tool>` routes each
otherwise-unresolved permission decision to a named MCP tool you provide. The
tool **receives** the tool name + input and **returns**, in its result text, a
JSON decision:

```jsonc
// what your permission MCP tool returns (in its content/text):
{ "behavior": "allow", "updatedInput": { /* original or modified args */ } }
// or
{ "behavior": "deny", "message": "why it was denied" }
```

- `behavior`, `updatedInput`, `message` field names are **verified** — they are
  the same `PermissionResult` shape the SDK `canUseTool` callback returns
  (TS: `{ behavior: "allow", updatedInput }` / `{ behavior: "deny", message }`).
- `updatedInput` lets the approver **rewrite args** before execution (e.g.
  sandbox a path) — verified in the SDK user-input doc.
- The exact **input** envelope the tool receives (`tool_name` + `input`) is
  consistently reported but the field names as delivered to a *stdio MCP* tool
  are **[UNVERIFIED]** at byte level — Anthropic's own issue #1175 confirms
  there is *no official minimal working example*. Treat the I/O shape as
  "PermissionResult out, {tool_name, input} in" and pin it with an integration
  test before relying on it.
- **Implication for talk2me:** this requires standing up a separate MCP server
  process whose tool blocks on a voice round-trip. Workable but heavy: the
  approval round-trip happens *inside* an MCP tool call, off the main
  stream-json channel, so talk2me would need a side channel from that MCP
  process back into the orchestrator to drive TTS/STT. High complexity.

#### Mechanism B — `--permission-prompt-tool stdio` + control protocol (host answers on stdin)

This is the mechanism that fits talk2me's existing single-process,
own-the-stdin architecture. Verified against the reverse-engineered
`claude-agent-sdk-go/docs/cli-protocol.md` (read verbatim via the GitHub API).

With `--permission-prompt-tool stdio`, the CLI emits a permission request **as a
control message on stdout** instead of prompting, and waits for the host to
**write a control response to stdin**:

CLI → host (stdout), one NDJSON line:

```json
{
  "type": "sdk_control_request",
  "request": {
    "subtype": "permission",
    "request_id": "perm_1",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf /tmp/test"}
  }
}
```

host → CLI (stdin), allow:

```json
{
  "type": "control_response",
  "response": {
    "subtype": "success",
    "request_id": "perm_1",
    "response": { "behavior": "allow" }
  }
}
```

host → CLI (stdin), deny:

```json
{
  "type": "control_response",
  "response": {
    "subtype": "success",
    "request_id": "perm_1",
    "response": { "behavior": "deny", "message": "Destructive commands are not permitted" }
  }
}
```

To modify args on allow, include `"updatedInput": {...}` inside the inner
`response` object (same `PermissionResult` shape as Mechanism A).

Notes / verification:

- Envelope fields (`type`, `request.subtype: "permission"`, `request_id`,
  `tool_name`, `tool_input`; response `subtype: "success"`, echoed
  `request_id`, inner `response.behavior`) are **verified** against the go-SDK
  protocol doc — but that doc is a **reverse-engineered community spec**, not
  Anthropic-official. The outer request type is reported as `sdk_control_request`
  there; one secondary summary reported `control_request` with subtype
  `can_use_tool`. **[PARTIALLY UNVERIFIED]** — the safe move is to **match
  defensively on both** (`type` in {`control_request`, `sdk_control_request`}
  and `request.subtype` in {`permission`, `can_use_tool`}) and to echo
  `request_id` back exactly.
- The CLI **blocks** on this request until a `control_response` with the
  matching `request_id` arrives — so an indefinitely-pending voice prompt is
  fine; the turn simply pauses. (The SDK `canUseTool` callback "can stay pending
  indefinitely" — verified — and stdio is the transport-level equivalent.)
- There is also an `initialize` control handshake
  (`{"type":"control_request","request":{"subtype":"initialize","request_id":...,"hooks":{...},"sdk_mcp_servers":[...]}}`)
  that the official SDKs send first. The bare CLI may not require it when you
  only use `--permission-prompt-tool stdio`; **[UNVERIFIED]** whether stdio
  permission routing works without sending `initialize`. Verify in the spike;
  if required, send a minimal `initialize` (empty hooks, empty servers) right
  after `start()`.

### 2.3 Mid-turn input on stdin generally

- Streaming input (`--input-format stream-json`) is explicitly designed to let
  you send messages **while Claude is processing** — verified ("guidance to the
  model while it is processing a request"). So writing a control_response
  mid-turn is consistent with the transport.
- There is **no documented mid-turn interrupt** on the bare CLI stdin
  (the backend's `interrupt()` is already a no-op for this reason). The go-SDK
  doc implies an interrupt control_request exists at the SDK layer;
  **[UNVERIFIED]** for the bare CLI. Barge-in stays a local-TTS-stop concern.

---

## 3. Design

### 3.1 Safe default posture for a voice coding assistant

A voice assistant has a slow, lossy, hands-free approval channel (speak the
request, run STT on a yes/no). That argues for: **make the common, safe path
need zero approvals, and make everything else an explicit spoken gate** — never
silent auto-execution of destructive or networked actions.

**Recommended default:** `--permission-mode default` **+ a curated
`allowedTools` allowlist** **+ a `disallowedTools` denylist for the sharp
edges**, with the stdio approval flow (3.2) handling the gap.

Rationale for `default` over the alternatives:

- `acceptEdits` silently runs `rm`/`mv`/`cp` inside cwd with no spoken
  confirmation — too much trust for a hands-free loop where Nate isn't watching
  a diff.
- `bypassPermissions` is off the table (full system access, inherited by
  subagents).
- `dontAsk` is attractive (hard deny, no stall) and is the right **fallback** if
  the stdio approval flow can't be wired — but as a primary it makes the agent
  unable to ever do a one-off allowed action by voice, which defeats "talk to my
  coder."
- `plan` is a good **explicit mode** to offer ("go into plan mode") but too
  restrictive as the always-on default.

**Proposed allowlist (auto-approve, no voice gate)** — read-only + safe local
inspection:

```
Read, Glob, Grep, TodoWrite,
Bash(ls *), Bash(cat *), Bash(pwd), Bash(git status *), Bash(git diff *),
Bash(git log *), Bash(git branch *), Bash(rg *), Bash(find *),
Bash(npm test *), Bash(npm run *), Bash(pytest *), Bash(python -m pytest *)
```

**Proposed denylist (hard-deny in every mode, never even ask)** — irreversible /
exfiltration / privilege:

```
Bash(rm -rf *), Bash(git push *), Bash(git reset --hard *),
Bash(curl *), Bash(wget *), Bash(sudo *), Bash(ssh *),
Bash(* | sh), Bash(dd *)
WebFetch  // optional — gate network reads behind voice if desired
```

Everything **not** in either list (Edit, Write, other Bash, MCP tools) falls
through to the **voice-approval gate**.

Config surface: `ClaudeCodeBackend.__init__` already takes `permission_mode`
and `extra_args`. Add the allow/deny lists via `extra_args`
(`--allowedTools`, `--disallowedTools`, `--permission-prompt-tool stdio`) or
promote them to named constructor params. Tools accept the
`Bash(prefix *)` permission-rule syntax (verified — note the space before `*`
for prefix matching: `Bash(git diff *)` not `Bash(git diff*)`).

### 3.2 Voice-approval flow (Mechanism B — stdio control protocol)

Primary design. One process, talk2me owns stdin, no extra MCP server.

**Flow:**

```
Claude wants tool X (not in allow/deny lists, default mode)
  → CLI emits {type: sdk_control_request, request:{subtype:permission, request_id, tool_name, tool_input}}
  → backend translates to a new event: PermissionRequest(request_id, tool_name, tool_input)
  → orchestrator: mute mic, SPEAK "Claude wants to run <spoken summary of X>. Say approve or deny."
  → unmute mic, listen one utterance, STT
  → intent match → APPROVE | DENY | (unclear → re-prompt once, then default DENY)
  → backend.respond_permission(request_id, allow|deny)
       writes control_response to stdin with the echoed request_id
  → CLI resumes the turn; normal AssistantTextDelta / ToolActivity / result follow
```

**Events to add** (`talk2me/events.py`) — keeps the closed event set honest:

- `PermissionRequest(request_id: str, tool_name: str, tool_input: dict)`

**Backend changes** (`talk2me/backends/claude_code.py`):

1. argv: add `--permission-prompt-tool`, `stdio`, plus `--allowedTools` /
   `--disallowedTools` from config. Keep `--permission-mode default`.
2. `_translate`: detect the control request defensively —

   ```
   if t in ("sdk_control_request", "control_request"):
       req = obj.get("request") or {}
       if req.get("subtype") in ("permission", "can_use_tool"):
           return [PermissionRequest(
               request_id=req.get("request_id"),
               tool_name=req.get("tool_name") or req.get("tool", ""),
               tool_input=req.get("tool_input") or req.get("input") or {},
           )]
   ```

   (Reads both reported field spellings so a version skew degrades to a prompt,
   not a crash — consistent with the file's existing defensive-parse contract.)
3. New method `respond_permission(request_id: str, allow: bool, *, message: str | None = None, updated_input: dict | None = None)`:

   ```
   inner = {"behavior": "allow"} if allow else {"behavior": "deny", "message": message or "Denied by voice"}
   if allow and updated_input is not None:
       inner["updatedInput"] = updated_input
   msg = {"type": "control_response",
          "response": {"subtype": "success", "request_id": request_id, "response": inner}}
   write json line to stdin; drain
   ```
   Add to the `AgentBackend` Protocol so the orchestrator stays
   provider-agnostic; non-supporting backends implement a no-op.
4. If the `initialize` handshake turns out to be required (3.2 caveat), send it
   in `start()` right after spawn and await the `control_response` success.

**Orchestrator changes** (`talk2me/orchestrator.py`, `_consume_turn`): add a
branch alongside the existing event handlers —

```
elif isinstance(ev, PermissionRequest):
    self.mic.set_muted(True)
    await self._speak(_phrase_permission(ev))      # "Claude wants to run npm install. Approve or deny?"
    self.mic.set_muted(False); self.vad.reset()
    decision = await self._listen_for_intent()      # one utterance → STT → intent
    await self.backend.respond_permission(ev.request_id, allow=(decision == "approve"))
    # do NOT break — keep consuming; the turn continues after the CLI resumes
```

**Spoken summary** (`_phrase_permission`): keep it short and intent-bearing, not
raw JSON. Map tool → phrase:

| Tool | Spoken as |
|---|---|
| `Bash` | "run the command <command, truncated to ~12 words>" |
| `Write` | "create the file <basename>" |
| `Edit` | "edit <basename>" |
| `mcp__*` | "use the <server> tool <tool>" |
| other | "use <tool_name>" |

**Intent match** (reuse STT; small grammar, no LLM round-trip needed):

- approve set: `approve, yes, yeah, go, do it, allow, sure, okay, confirm`
- deny set: `deny, no, nope, stop, don't, cancel, reject, skip`
- ambiguous / empty → re-ask once with "I didn't catch that — approve or deny?"
  → still ambiguous → **deny** (safe default; never auto-allow on uncertainty).
- Seed the whisper bias toward this grammar the same way the VAD+whisper path is
  already aliases-seeded (see `project_talk2me_voice_broker`).

### 3.3 Fallback if stdio approval can't be wired

If the spike shows `--permission-prompt-tool stdio` isn't honored by the bare
CLI build in use, or the `initialize` handshake proves mandatory and brittle,
fall back to a **no-mid-turn-approval** posture that is still safe:

- Run `--permission-mode dontAsk` with the **allowlist** from 3.1.
- Any tool outside the allowlist is **hard-denied by the CLI** (no stall, no
  silent execution). Claude sees the denial and either reroutes or reports it.
- talk2me detects the denial in the `result` / assistant stream and **speaks**
  it: "Claude tried to run X but it's not allowed — say 'allow X' to enable it
  for this session." On that spoken consent, talk2me **restarts the backend**
  with X added to `allowedTools` (or, once verified, calls a dynamic
  `set_permission_mode` / appends an allow rule). This trades a turn restart for
  protocol simplicity and never auto-runs anything ungated.
- This is strictly weaker UX (deny-then-reauthorize vs. inline approve) but is
  the conservative, fully-verified path: `dontAsk` + `allowedTools` semantics
  are confirmed in Anthropic's own docs; the stdio control envelope is not.

### 3.4 Why not `acceptEdits` even as a convenience

Tempting to default-on `acceptEdits` so file edits flow without gating. Rejected
for the hands-free case: it auto-runs `rm`/`mv`/`cp`/`sed` inside cwd with no
spoken confirmation, and is inherited by subagents. A voice user who isn't
watching the screen can't catch a bad edit before it lands. Offer it as an
explicit spoken mode switch ("turn on accept edits") gated on confirmation, not
as the default.

---

## 4. Open items to verify in a spike (before trusting the inline path)

1. **Exact control envelope.** Send a tool through `--permission-prompt-tool
   stdio` and capture stdout. Confirm `type` (`sdk_control_request` vs
   `control_request`) and `request.subtype` (`permission` vs `can_use_tool`) and
   the inner field names (`tool_input` vs `input`). Pin with a fixture test.
   **[UNVERIFIED at byte level]**
2. **`initialize` requirement.** Does stdio permission routing work without the
   `initialize` control handshake? If not, wire a minimal one.
   **[UNVERIFIED]**
3. **Blocking semantics.** Confirm the CLI truly pauses the turn until the
   matching `control_response` lands, with no timeout shorter than a voice
   round-trip (the 60s figure floating in summaries is reported for *MCP tool*
   responses, not necessarily permission responses — **[UNVERIFIED]**; if a
   timeout exists, speak fast and consider a "thinking…" filler).
4. **`updatedInput` honored on the bare CLI** (vs only the SDK). Low priority —
   talk2me's v1 can allow/deny without rewriting args.
5. **MCP-tool naming for the denylist.** Confirm `mcp__server__*` matches in
   `disallowedTools` on this CLI version.

These are all behavioral confirmations achievable with one scripted
`claude -p` run piped through `tee`; none require shipping code.

---

## 5. Summary of the recommendation

- **Default:** `--permission-mode default` + curated `--allowedTools`
  (read-only + safe inspection) + `--disallowedTools` (irreversible / network /
  privilege) + `--permission-prompt-tool stdio`.
- **Inline voice approval:** detect `subtype:"permission"` control requests on
  stdout, speak a short summary, STT a yes/no, write a `control_response` with
  the echoed `request_id` to stdin. Add a `PermissionRequest` event and a
  `respond_permission()` backend method; add one branch to `_consume_turn`.
- **Ambiguity → deny.** Never auto-allow on an unclear utterance.
- **Fallback** (if stdio control proves unsupported): `--permission-mode
  dontAsk` + allowlist; speak denials and re-authorize by restarting the backend
  with the tool added.
- **Confirm the wire shape with a one-shot spike** before depending on the
  control envelope — Anthropic has no official minimal example, and the envelope
  is corroborated only by a reverse-engineered community spec.

---

## Sources

Verified primary (Anthropic):
- Run Claude Code programmatically (headless / stream-json / flags / `dontAsk` / `acceptEdits`): https://code.claude.com/docs/en/headless
- Configure permissions (modes, allow/deny rule semantics, evaluation order, subagent inheritance, MCP + acceptEdits): https://code.claude.com/docs/en/agent-sdk/permissions
- Handle approvals and user input (`canUseTool`, `PermissionResult` allow/deny/`updatedInput`/`message`, AskUserQuestion): https://code.claude.com/docs/en/agent-sdk/user-input
- Connect to external tools with MCP (`mcp__server__tool` naming, allowedTools wildcards): https://code.claude.com/docs/en/agent-sdk/mcp

Reverse-engineered / community (corroborating the stdio control envelope — treat as **[PARTIALLY UNVERIFIED]**):
- claude-agent-sdk-go CLI control protocol (read verbatim; source of the `sdk_control_request` / `control_response` permission shapes): https://github.com/Roasbeef/claude-agent-sdk-go/blob/main/docs/cli-protocol.md
- `--permission-prompt-tool` has no official minimal example (Anthropic issue): https://github.com/anthropics/claude-code/issues/1175
- Wrapping the Claude CLI for agentic apps (mode/flag overview): https://avasdream.com/blog/claude-cli-agentic-wrapper
- Claude stream-json event cheatsheet (event types): https://takopi.dev/reference/runners/claude/stream-json-cheatsheet/

talk2me code read (read-only):
- `/Users/nathandavidovich/code/talk2me/talk2me/backends/claude_code.py`
- `/Users/nathandavidovich/code/talk2me/talk2me/orchestrator.py`
- `/Users/nathandavidovich/code/talk2me/talk2me/protocols.py`
