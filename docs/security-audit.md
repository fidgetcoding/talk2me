# talk2me — Security Audit

Date: 2026-05-22
Scope: `/Users/nathandavidovich/code/talk2me` — full source read.
Reviewer: security-auditor (read-only; no code changes, recommendations only).
Trust model: talk2me is a local, single-user CLI voice broker. The operator is
trusted. The **untrusted inputs** are: (1) transcribed microphone audio (whisper
output) that becomes user text fed to `claude`, (2) `claude` stdout/stderr JSON
the broker parses, and (3) a `--vocab-file` path/contents the operator points at.
The most security-relevant property is that this process spawns two child
programs (`say`, `claude`) and parses untrusted process output — so the audit
centers on argument injection, tempfile hygiene, file-path handling, parser DoS,
and the permission posture handed to `claude`.

---

## Summary

| Severity | Count | Items |
|---|---|---|
| Blocker | 0 | — |
| High | 2 | H1 vocab-file DoS · H2 stdout/stderr unbounded readline |
| Medium | 4 | M1 vocab-file traversal/read · M2 permission-mode passthrough · M3 extra_claude_args passthrough · M4 say tempfile leak on synth failure |
| Low | 3 | L1 stderr error-keyword leakage · L2 vocab → whisper prompt-injection · L3 mkstemp default mode note |

No Blocker-class findings. The two subprocess call sites are both built
correctly (list-form argv, no shell). The real exposure is resource exhaustion
on untrusted streams and the permissions the broker is willing to forward to
`claude`.

---

## What's already done right (keep it)

These are deliberate, correct decisions. Do not regress them.

- **`say` is invoked safely.** `say.py:46-55` builds a **list** argv and calls
  `subprocess.run(argv, check=True, capture_output=True)` with **no
  `shell=True`**. The synthesized text is passed as a **single positional arg
  after `--`** (`say.py:51`: `argv += ["--", text]`), so even if whisper
  transcribes `; rm -rf ~` or `-v evil`, it cannot become a flag or a shell
  metacharacter — it is one opaque argv element. This is exactly the right
  pattern.
- **`claude` is invoked safely.** `claude_code.py:88-94` uses
  `asyncio.create_subprocess_exec(*self._argv(), ...)` — list-form, no shell.
  User turn text never touches argv at all; it is serialized into a JSON line
  and written to **stdin** (`claude_code.py:102-111`), which is the safest
  possible channel — no quoting, no flag confusion.
- **stdout JSON parsing is defensive.** `claude_code.py:156-159` wraps
  `json.loads` in a try/except that swallows `JSONDecodeError` and continues;
  `_translate` (`:187-209`) treats unknown event shapes as no-ops; the reader's
  outer `except Exception` (`:164-165`) converts any parser blow-up into a
  `BackendError` event instead of killing the loop. A malformed or hostile line
  cannot crash the broker.
- **Session store is hardened.** `session.py` validates UUID shape on save
  (`:114-115`), writes atomically via temp-file-in-same-dir + `os.replace`
  (`:130-139`), cleans up the temp file on any exception (`:140-147`), and
  treats a corrupt/unreadable store as "no session" rather than raising
  (`:150-177`). `$TALK2ME_HOME` is `.expanduser()`-resolved. This module is the
  template the rest of the codebase should follow.
- **No secrets in source.** No API keys, tokens, or credentials are hardcoded
  anywhere. `claude` owns its own auth out of process; talk2me never reads or
  forwards credentials. `.env` is not referenced.
- **No network surface.** STT (faster-whisper) and TTS (`say`/Kitten) are fully
  local. There is no inbound listener and no SSRF surface.

---

## High

### H1 — `--vocab-file` reads an unbounded file into memory (DoS)

File: `talk2me/__main__.py:58-64`

```python
def _collect_vocab(terms, path):
    out = list(terms)
    if path:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                out += [t.strip() for t in line.split(",") if t.strip()]
    return out
```

Line-iteration is streaming, but there is **no cap on file size, line count, or
line length**. Pointing `--vocab-file` at `/dev/zero`, a multi-GB file, or a file
with no newlines forces an unbounded read into a Python list (and, downstream,
into a single whisper `initial_prompt` string). A file with no `\n` makes
`for line in fh` buffer the entire file as one "line" before splitting.

This is operator-supplied, so the blast radius is "operator shoots own foot,"
which keeps it out of Blocker territory — but a 2ndBrain user is told to feed
`aliases.yaml`-derived term files in, and an accidentally-huge or malformed file
hangs/OOMs the process before the voice loop ever starts.

**Fix (recommend):**
- Stat the file first and refuse over a sane ceiling (e.g. 1 MB).
- Cap the number of accumulated terms (e.g. first 5,000) and the per-term
  length, then `break`.
- Bound the read with `fh.read(MAX_BYTES)` instead of unbounded line iteration,
  or read line-by-line with a max-line-length guard.
- Separately cap the assembled `initial_prompt` length in `factory._vocab_prompt`
  (`factory.py:67-75`) — whisper's prompt is itself length-limited, and an
  enormous prompt is wasted compute at best.

### H2 — Unbounded `readline()` on untrusted `claude` stdout/stderr (memory DoS)

File: `talk2me/backends/claude_code.py:150` (stdout) and `:176` (stderr)

```python
raw = await self._proc.stdout.readline()   # :150
...
raw = await self._proc.stderr.readline()    # :176
```

`asyncio.StreamReader.readline()` accumulates until a newline or EOF, bounded
only by the stream's internal limit (default 64 KiB) — past which it raises
`LimitOverflowError` / `ValueError`. A `claude` build (or a compromised/buggy
binary, or a future MCP tool that echoes attacker-influenced text) that emits a
single line larger than that limit will throw inside `_read_stdout`. The outer
`except Exception` (`:164-165`) catches it and posts a `BackendError`, so it
degrades rather than crashes — good — but the loop then **stops reading that
stream**, wedging the conversation. There is no per-line byte ceiling and no
recovery/resync after an overlong line.

Because `claude` is normally trusted-ish, this is High not Blocker — but the
broker's entire design premise is "treat stream-json defensively," and an
unbounded line read is the one place that premise leaks.

**Fix (recommend):**
- Construct the subprocess with an explicit, generous `limit=` on the
  `StreamReader` (e.g. `asyncio.create_subprocess_exec(..., limit=2**20)`), or
- Switch to a chunked `read(n)` + manual newline-splitting loop that caps the
  in-flight buffer and drops/resyncs on an over-long line instead of throwing,
- Apply the same bound to the stderr drain (`:171-185`).

---

## Medium

### M1 — `--vocab-file` accepts an arbitrary path (traversal / arbitrary read)

File: `talk2me/__main__.py:32-35` (flag) → `:60-63` (open)

`--vocab-file` is `open(path, ...)` with zero validation — no realpath/allowlist,
no symlink check, no "must be a regular file" guard. The operator can point it at
`/etc/passwd`, `~/.ssh/id_rsa`, or a symlink, and every comma/line-split token
becomes a whisper bias term. Since talk2me runs with the operator's own
privileges and the operator chose the path, this is **self-directed**, not a
privilege boundary crossing — hence Medium. The genuine risk is a *config-driven*
invocation (a wrapper script, a 2ndBrain skill, an alias) passing an
attacker-influenced path, which would turn into an arbitrary-file read whose
contents leak into the whisper prompt and indirectly into the model's context.

**Fix (recommend):**
- `Path(path).expanduser().resolve()` then assert it is a regular file
  (`.is_file()`, reject symlinks via `os.path.realpath` comparison if you want to
  be strict).
- If talk2me ever grows a config-file or non-operator invocation path, constrain
  vocab files to a known directory (e.g. under `$TALK2ME_HOME`).
- Combine with H1's size cap in the same guard.

### M2 — `--permission-mode` is forwarded to `claude` verbatim; `bypassPermissions` would be dangerous

File: `talk2me/__main__.py:21` → `config.py:14` → `claude_code.py:79-81`

```python
argv += ["--permission-mode", self._permission_mode]
```

`--permission-mode` is a free-form string with **no validation or allowlist** —
whatever the operator types is handed straight to `claude`. Today the default is
`"default"` (safe). The task flags the wisdom of `bypassPermissions` "if that
lands later," and the answer is: **it should never be the default, and ideally
never reachable from a voice path without a loud, explicit gate.**

Why this matters specifically for a *voice* broker:
- The user text driving `claude` is **whisper output** — a probabilistic
  transcription of room audio. It mishears, splices background speech, and can be
  influenced by anything audible (a podcast, a colleague, a TV, a maliciously
  crafted audio clip played near the mic).
- `claude` runs with `--cwd` and whatever tools/MCP servers the user's
  environment grants. Under `bypassPermissions`, every tool call — file writes,
  `Bash`, `git push`, MCP mutations — executes with **no human confirmation**.
- Combine the two and `bypassPermissions` means "any sound near the laptop can
  silently run shell commands and mutate the filesystem/repos." That is a
  full RCE-by-ambient-audio posture. The barge-in/half-duplex design does not
  mitigate it; muting the mic during playback doesn't stop a hostile utterance
  *before* playback.

**Fix (recommend):**
- Allowlist the flag now: accept only `{default, plan, acceptEdits}` (and
  whatever the installed `claude` actually supports), reject everything else with
  a clear error. `argparse(choices=[...])` is the cheapest enforcement.
- If `bypassPermissions` is ever supported, require a separate explicit opt-in
  flag (e.g. `--i-understand-bypass`) **and** print a persistent warning banner,
  **and** consider refusing it entirely in `voice` mode (allow only in `--text`
  mode where input is keyboard, not ambient audio).
- Document the threat in the README so the trade-off is a conscious operator
  choice, not a default.

### M3 — `claude_args` / `extra_claude_args` passthrough is unfiltered

File: `talk2me/__main__.py:40` (`claude_args`, `nargs="*"`) → `config.py:43` →
`claude_code.py:84` (`argv += self._extra_args`)

Trailing positional args are appended verbatim to the `claude` argv. This is an
intentional escape hatch and is **argv-safe** (list form, no shell) — an operator
passing extra `claude` flags is expected behavior, so this is not injection.

The Medium concern is **interaction with M2 and the broker's invariants**: a user
(or a wrapper) can smuggle `--permission-mode bypassPermissions`,
`--dangerously-skip-permissions`, or `--add-dir /` through the trailing args even
if the dedicated `--permission-mode` flag is later allowlisted, because
`extra_args` is appended *after* the broker's own flags and is never inspected.
The broker advertises "we own stdin / we filter output" as safety guarantees;
an unfiltered argv tail quietly undoes the permission posture those guarantees
assume.

**Fix (recommend):**
- Scan `extra_claude_args` for permission-affecting flags
  (`--permission-mode`, `--dangerously-skip-permissions`, `--add-dir`, any
  future bypass switch) and either reject them or route them through the same
  allowlist as M2.
- At minimum, document that trailing args bypass the broker's permission guard,
  so the escape hatch is understood.
- Note that argparse `nargs="*"` collects unknown trailing tokens; verify a typo'd
  broker flag can't silently fall through into the `claude` argv with a
  surprising effect.

### M4 — `say` tempfile can leak when synthesis fails before the read phase

File: `talk2me/tts/say.py:43-56` (`_render`) and `:33-41` (`synthesize`)

`_render` creates the temp WAV with `mkstemp` (`:44`), then runs `say` with
`check=True` (`:55`). If `say` fails (`CalledProcessError`) or the worker thread
is cancelled between `mkstemp` and a successful return, `_render` raises and the
**path is never returned to `synthesize`** — so the `finally: os.unlink(path)`
cleanup in `synthesize` (`:37-41`) is keyed on a `path` it never received and the
zero-byte temp file is orphaned in `$TMPDIR`. Over a long session with repeated
TTS errors this accrues junk files. Same exposure if `wave.open` in
`_read_blocks` throws on a malformed file `say` produced.

This is hygiene, not a security boundary crossing (the file is created by
`mkstemp` with a random name and `0600` perms — see L3), so Medium-low.

**Fix (recommend):**
- Wrap the `subprocess.run` in `_render` in try/except and `os.unlink(path)` on
  failure before re-raising, **or** move tempfile creation and the unlink into a
  single `try/finally` (or a context manager) inside `_render` so the file's
  lifetime is owned by the function that creates it.
- Prefer a `tempfile.NamedTemporaryFile(delete=True)` / explicit cleanup wrapper
  so cleanup is structural, not dependent on the return value reaching the caller.

---

## Low

### L1 — stderr "error keyword" surfacing can echo untrusted text into the transcript

File: `talk2me/backends/claude_code.py:178-181`

```python
if txt and ("error" in txt.lower() or "fatal" in txt.lower()):
    await self._events.put(BackendError(f"stderr: {txt}"))
```

Any stderr line containing "error"/"fatal" is forwarded into a `BackendError` and
printed to the user's terminal (`orchestrator.py:106-107`). stderr is untrusted
output from `claude`; a crafted or noisy line could inject ANSI escape sequences
or terminal control characters into Nate's terminal via the printed message. Low
because it's local stdout and `claude` is broadly trusted, but terminal-escape
injection from a child process's stderr is a known footgun.

**Fix (recommend):** sanitize/escape control characters before printing
`BackendError.message` (strip `\x1b[`, non-printable bytes). Optionally cap the
forwarded length. The keyword heuristic also risks false positives (any normal
log line mentioning "error") — fine functionally, just noted.

### L2 — Vocab terms flow unescaped into the whisper `initial_prompt`

File: `talk2me/factory.py:67-75`

```python
return "Proper nouns and domain terms: " + ", ".join(vocab) + "."
```

Vocab terms (from `--vocab` or `--vocab-file`) are concatenated directly into the
whisper prompt with no length bound or content sanitation. This is a *prompt* to
a local STT model, not a shell or SQL context, so there's no classic injection —
the worst case is a vocab file that steers transcription oddly or (with H1) bloats
the prompt. Worth pairing the length cap here with the H1/M1 fixes. Documented as
Low for completeness; no exploit path beyond "garbage in, garbage transcription."

### L3 — Document `mkstemp` permission/name guarantees (informational)

File: `talk2me/tts/say.py:44`, `talk2me/session.py:130`

Both `mkstemp` call sites are **correct**: `mkstemp` creates the file atomically
with `O_EXCL`, a randomized (non-predictable) name, and mode `0600` (owner-only),
which closes the predictable-name and symlink-race classes the task asked about.
There is no symlink race here and names are not predictable. This entry exists
only to record that the audit checked it and found it sound — no change needed.
(The `say.py` site does `os.close(fd)` immediately and then lets `say` write the
path; because the name is random and `$TMPDIR` is typically `0700` per-user on
macOS, the brief window between `os.close` and `say` opening it is not a
practical race for a single-user local tool. If you want belt-and-suspenders,
keep the fd and pass it to `say` — but `say -o` wants a path, so this is
acceptable as-is.)

---

## Cross-cutting notes

- **No `shell=True` anywhere.** Both `subprocess.run` (`say.py:55`) and
  `create_subprocess_exec` (`claude_code.py:88`) are list-form. This is the
  single most important thing and it's correct throughout.
- **Untrusted-text taint path:** mic audio → whisper → user text → `claude`
  stdin (safe, JSON-on-stdin) → `claude` stdout JSON → parsed defensively →
  assistant text → `say` argv-after-`--` (safe). The taint never reaches a shell
  or an unquoted argv slot. The remaining risk is *semantic*: whisper output
  drives an LLM that may have powerful tools — which is exactly why M2
  (permission posture) is the most important finding to act on before any
  `bypassPermissions` work lands.
- **Tests** (`tests/`) use fakes and offline loops; no security-relevant code
  paths are exercised for the subprocess or file-read surfaces. Adding a test
  that feeds an oversized `--vocab-file` and a >64 KiB stdout line would lock in
  the H1/H2 fixes.

## Priority order for fixes

1. **M2** — gate `--permission-mode` / forbid silent `bypassPermissions` (highest
   real-world impact given the voice → tool-execution chain).
2. **H1 + M1 + L2** — one combined `--vocab-file` guard: size cap + path
   validation + prompt-length cap.
3. **H2** — bound the stdout/stderr line reader.
4. **M3** — filter permission flags out of the `extra_claude_args` tail.
5. **M4** — own the `say` tempfile lifetime inside `_render`.
6. **L1** — sanitize control chars before printing `BackendError`.
