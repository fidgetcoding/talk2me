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

---

# Denylist Spike (2026-11-01, claude CLI 2.1.296)

What do `--disallowedTools` rules really stop? Same rule as above: observed, not
inferred. `tests/manual_denylist_check.py` is the rerunnable version. A local fake
Anthropic endpoint answers each session with a `tool_use` for the exact command, so
no model can reword or refuse it and the CLI's matcher is the only thing deciding.
Verdicts come from the `permission_decision` the CLI records on each tool result.

## How the matcher behaves

| Rule shape | Behavior |
|---|---|
| `Bash(rm -rf:*)` | Word-bounded prefix. Stops `rm -rf d`, misses `rm -fr d`, `rm -Rf d`, `rm -r -f d`, `/bin/rm -rf d`. |
| `Bash(nc:*)` | Word boundary holds: stops `nc -h`, leaves `ncal` alone. Same for `su:*` and `sum`. |
| `Bash(rm -r*)` | `*` with no colon is a real glob. Stops `rm -rf`, `rm -r d`, `rm -r -f d`. |
| `Bash(*/curl *)` | A leading `*` works, spaces included. Stops `/usr/bin/curl --version`, and any command containing that text, like `ln -s /usr/bin/curl c`. |
| `Bash(python3)` | No wildcard means exact: stops a bare `python3` on the end of a pipe, leaves `python3 --version` alone. |

The CLI does real parsing before it matches, which is why these need no rules of their
own. With only `Bash(rm -rf:*)` listed, all of these were stopped: `true && rm -rf d`,
`echo hi; rm -rf d`, `echo d | xargs rm -rf`, `sh -c 'rm -rf d'`, `command rm -rf d`,
`env rm -rf d`, `timeout 5 rm -rf d`.

Deny beats allow. Under `--permission-mode default` with `Bash(find:*)` allowlisted,
`find . -name marker -delete` was stopped and `find . -name marker` ran.

## The v2.5 denylist, tested as shipped

8 prefix rules, under `bypassPermissions`. The audit's reading was right:

| Stopped | Ran |
|---|---|
| `rm -rf d` | `rm -fr d`, `rm -Rf d`, `rm -r -f d`, `/bin/rm -rf d` |
| `curl --version` | `/usr/bin/curl --version` |
| `git push` | `git -C . push`, `git --no-pager push` |
| `sudo -n true` | `find . -name marker -delete`, `python3 -c 'print(1)'`, `scp`, `nc -h` |

## The v2.6 denylist

69 rules, in `talk2me/config.py`. 72 of 72 checks came out as expected: 55 commands
stopped, 9 everyday commands untouched (`rm -f marker`, `git commit -m 'push it'`,
`python3 --version`), 5 known gaps confirmed still open, and the 3 allow-versus-deny
cases above.

## What a denylist can't guarantee

These ran under the v2.6 rules, and they're in the check on purpose as `KNOWN_GAPS`:

- `rm -vrf d`: a flag spelling nobody listed
- `chmod +x marker && ./marker`, or any script: the rule reads the command line, not the file
- `git config alias.p push && git p`: a git alias
- `npm run nothing`: whatever the project defines

Matching text can never settle what a command does. It raises the bar for an
accident or a mishearing, and it won't stop anyone determined, or an agent that
writes a script. The gate is the real control. `SECURITY.md` says this for users.
