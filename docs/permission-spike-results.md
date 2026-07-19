# Permission Wire Spike — Results (2026-07-18, claude CLI 2.1.214)

Live confirmation of the open items in `permission-handling-design.md §4`.
Every claim below was observed against the installed CLI, not inferred.

## Verdict: Mechanism B (stdio control protocol) WORKS. Build the inline flow.

## Pinned wire shapes (byte-level)

CLI → host, permission request (one NDJSON line on stdout):

```json
{
  "type": "control_request",
  "request_id": "edf969bc-ebfd-49a0-873d-698f4a2336a9",
  "request": {
    "subtype": "can_use_tool",
    "tool_name": "Bash",
    "display_name": "Bash",
    "input": {"command": "touch spike-test.txt", "description": "Create spike-test.txt file"},
    "description": "Create spike-test.txt file",
    "permission_suggestions": [ ... ]
  }
}
```

Resolves the design doc's ambiguities:

- outer `type` is `control_request` (NOT `sdk_control_request`)
- `request_id` is **top-level**, not inside `request`
- `request.subtype` is `can_use_tool` (NOT `permission`)
- the args field is `input` (NOT `tool_input`)

Keep the defensive both-spellings parse anyway — it costs nothing and survives
version skew.

host → CLI, response (stdin):

```json
{"type": "control_response",
 "response": {"subtype": "success",
              "request_id": "<echoed exactly>",
              "response": {"behavior": "allow"}}}
```

Deny: `{"behavior": "deny", "message": "Denied by voice"}`.

## Behavioral confirmations

| Question (design §4) | Result |
|---|---|
| `--permission-mode default` still accepted? | Yes — help lists `manual` etc. but `default` parses and behaves (no approver → tool denied, turn completes cleanly; **no stall**). |
| `initialize` handshake required? | **No.** stdio permission routing works with zero handshake. |
| Blocking semantics / timeout | CLI blocked ≥20 s on an unanswered request, then honored the allow and ran the tool. **No observed timeout** — a voice round-trip is safe. |
| Deny path | Turn resumes; Claude *speaks the denial* ("The command was denied… Would you like to approve running `touch spike-test.txt`?") — free re-approval loop via the next user turn. |
| Host → CLI `interrupt` | **WORKS.** `{"type":"control_request","request_id":"…","request":{"subtype":"interrupt"}}` → CLI acks with a `control_response` (`{"still_queued": []}`) and ends the turn with `result subtype=error_during_execution is_error=true`. Generation actually stops. |
| Session after interrupt | Alive — a follow-up user turn on the same process answers normally. `backend.interrupt()` can be real, not a no-op. |
| allowlist rule syntax | Both `Bash(touch:*)` and `Bash(touch *)` auto-allow on 2.1.214. talk2me uses the currently-documented `:*` form. |

## Spike scripts

Driver scripts lived in the session scratchpad (`perm_spike.py`, `perm_spike2.py`);
the essential technique: spawn `claude -p --input-format stream-json
--output-format stream-json --include-partial-messages --verbose --model haiku
--permission-mode manual --permission-prompt-tool stdio` in a temp cwd, write a
user turn asking for `touch spike-test.txt`, read stdout lines, answer the
control request, then check the marker file.
