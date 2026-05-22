# talk2me — Code Review

Scope: `talk2me/*` + `tests/*`, focus on `orchestrator.py`, `audio.py`,
`backends/claude_code.py`, `segment.py`, `tts/say.py`. Read-only review — no
code was modified. Severity grouped: **Blocker** (will break a real run / loses
user data) · **Issue** (correctness or robustness gap, fix before relying on
it) · **Nit** (polish, low risk).

Overall: the architecture is clean — the Protocol seam holds, the events
generator-reuse pattern is correct, and the half-duplex contract is well tested.
The findings below are concentrated in error/EOF/edge paths that the happy-path
tests don't exercise.

---

## Blockers

### B1 — BackendError leaves a dead backend in the loop; next turn re-sends into a corpse
`orchestrator.py:106-108` (the `BackendError` branch) + `run()` loop at
`orchestrator.py:61-71`.

On `BackendError`, `_consume_turn` only does `break`. Control returns to the
`async for utterance in segment_utterances(...)` loop in `run()`, which keeps
listening and on the next utterance calls `await self.backend.send(text)`
(line 69) against a backend whose `claude` process has already exited (that is
exactly when `_read_stdout`/`_drain_stderr` emit `BackendError` — see
`claude_code.py:165,169,181`).

`ClaudeCodeBackend.send` (`claude_code.py:98-111`) guards only `self._proc is
None`; after the child dies, `self._proc` is non-None but its `stdin` transport
is closed, so `stdin.write` / `drain()` raise `ConnectionResetError` /
`BrokenPipeError` — an unhandled exception that escapes `run()` and tears down
the whole app with a traceback instead of a clean "agent died, exiting" message.
Worse, if the pipe happens to still accept the write, the user talks into a void
forever: no events ever arrive because the reader task already hit EOF and
returned.

**Fix:** make `BackendError` terminal for the run. Re-raise a typed
`BackendDownError` out of `_consume_turn`, or set a flag the `run()` loop checks
to `break` out of the segmentation loop and proceed to the `finally` teardown.
Minimal version:

```python
elif isinstance(ev, BackendError):
    print(f"\n[backend error] {ev.message}", flush=True)
    self._backend_dead = True   # set in __init__ = False
    break
```
and in `run()`:
```python
async for utterance in segment_utterances(...):
    ...
    await self._consume_turn(events)
    if self._backend_dead:
        break
    print("\n🎧 listening…", flush=True)
```
The same applies to `__main__.py:116-118` (`_run_text`) — there `BackendError`
breaks the inner loop but the outer `while True` immediately reads another stdin
line and re-sends into the dead backend.

---

## Issues

### I2 — `Mic._loop = asyncio.get_event_loop()` captures the wrong (or no) loop
`audio.py:28`.

`Mic.__init__` runs at construction time (`__main__.py:77`,
`_run_voice` → `Mic(...)`). Today that happens to be inside `asyncio.run(...)`
so a running loop exists, but `get_event_loop()` is the deprecated API:
under Python 3.12+ it warns, and if a `Mic` is ever constructed off-loop (a test,
a future eager-build path) it silently creates a *new* loop that is never run —
then `call_soon_threadsafe` (`audio.py:37`) schedules `put_nowait` onto a loop
that never ticks, and `frames()` blocks forever on an empty queue.

The audio callback fires on a PortAudio thread, so you genuinely need the loop
reference. The robust pattern is to bind it at `start()` (which is always called
from the running loop in `run()`), not at construction:

```python
def start(self) -> None:
    self._loop = asyncio.get_running_loop()
    self._stream = sd.InputStream(...)
    self._stream.start()
```
Drop the `self._loop = asyncio.get_event_loop()` line from `__init__`. This
also tightens the contract: the queue and the loop are bound at the same moment
the stream starts producing.

### I3 — `say.py` leaks the temp WAV when `say` fails
`tts/say.py:29-41` + `_render` at `:43-56`.

`synthesize` calls `path = await asyncio.to_thread(self._render, text)`
*before* entering the `try/finally` that unlinks `path`. `_render` does
`mkstemp` (creating the file) and then `subprocess.run(argv, check=True)`. If
`say` exits non-zero — bad `--voice`, unwritable temp dir, a text payload `say`
chokes on — `check=True` raises `CalledProcessError` from inside the thread,
which propagates out of the `await` and **skips the `finally`**, leaking the
0-byte temp file in `$TMPDIR` on every failed synthesis. Over a long session
with a misconfigured voice this is a slow temp-dir fill.

**Fix:** clean up inside `_render` on failure, or move the `mkstemp` so the
unlink is guaranteed:
```python
def _render(self, text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="t2m_say_")
    os.close(fd)
    try:
        subprocess.run(argv, check=True, capture_output=True)
    except BaseException:
        try: os.unlink(path)
        except OSError: pass
        raise
    return path
```
Separately, a failed `say` currently raises straight up through `_speak` →
`_consume_turn` → `run()` and crashes the app. Synthesis failure should
degrade (skip the sentence, log it), not kill the conversation — consider
catching `CalledProcessError` in `_render` and returning empty/`None`.

### I4 — `_translate_assistant_message` produces total silence if partials are absent
`claude_code.py:226-236` + comment at `:60-62`.

The design is correct *when* `--include-partial-messages` is on: text streams
via `stream_event` deltas (`:211-219`) and the full `assistant` message handler
deliberately emits only `ToolActivity` to avoid double-speak. Good.

But the docstring at `:227` ("Fallback path when partials are absent") and the
comment at `:60` both claim a fallback exists — it does not. If a `claude`
version ever stops emitting `content_block_delta` stream events (version drift
is explicitly anticipated in the module docstring, `:15-16`), `_turn_text` stays
empty, no `AssistantTextDelta` is ever emitted, the assistant message handler
drops the text on the floor, and `TurnComplete` rolls up `""`. The user hears
nothing and the loop silently hands the mic back — a confusing dead-air failure
that looks like a mic problem.

**Fix:** make the fallback real. Track whether any delta arrived this turn; if
the `assistant` message lands with text blocks and no deltas were seen, emit the
text from there. A simple guard: a `self._saw_delta` bool reset in `send()`, set
in `_translate_stream_event`, checked in `_translate_assistant_message` before
deciding whether to emit text blocks. At minimum, fix the misleading comments so
the next reader doesn't trust a fallback that isn't wired.

### I5 — trailing speech with no closing silence is lost on stream end
`segment.py:36-70`.

An utterance is only emitted inside the `trailing_silence >=
silence_frames_needed` branch (`:54-64`). If the frame stream ends while
`in_speech` is `True` and trailing silence never reaches the threshold — the
`async for` simply exhausts — the buffered speech is **never yielded**. The
final utterance is silently dropped.

For the live mic this is mostly theoretical (the queue never ends), but it bites
in two real places:
- Any finite/bounded `frames()` source (the `--text`-style or test rigs, future
  file-replay input). `tests/test_segment.py:39` only ever feeds streams that
  end in silence, so this gap is untested.
- A mic that's stopped (`Mic.stop()`) mid-utterance during shutdown.

**Fix:** flush after the loop:
```python
async for frame in frames:
    ...
# stream ended — flush a final utterance if we were mid-speech and it qualified
if in_speech and speech_frames >= min_speech_frames:
    yield np.concatenate(buf).astype(np.float32)
```
Add a `test_segment` case where the stream ends on speech to lock this in.

### I6 — `_drain_stderr` error heuristic both false-positives and false-negatives
`claude_code.py:171-185`.

The substring test `"error" in txt.lower() or "fatal" in txt.lower()`
(`:180`) is fragile in both directions:
- **False positive:** Claude Code logs plenty of benign lines containing
  "error" (e.g. "0 errors", "retrying after error", a tool printing the word).
  Each one becomes a `BackendError` event — and per **B1**, a single
  `BackendError` is currently fatal to the turn. So a harmless stderr line can
  kill an otherwise-healthy conversation.
- **False negative:** a genuine crash that writes to stderr without the literal
  words "error"/"fatal" (a Python traceback's `Traceback (most recent call
  last):` header, an OOM kill, a `panic`) is swallowed silently (`:184` bare
  `except Exception: pass`).

The reliable death signal is already captured correctly: process exit with
non-zero `returncode` in `_read_stdout`'s `finally` (`:166-169`). Lean on that
as the source of truth for "backend died." Treat stderr as advisory: buffer the
last N lines and attach them to the exit-based `BackendError` for diagnostics,
rather than synthesizing independent error events from substring matches.

### I7 — `close()` cancels reader tasks but never awaits them
`claude_code.py:127-142`.

`close()` calls `self._reader_task.cancel()` / `self._stderr_task.cancel()`
(`:128-131`) but never awaits the tasks. Cancellation is cooperative — the tasks
are mid-`await readline()` and won't observe the `CancelledError` until the loop
gets a chance to run them. Since `close()` returns and `run()`'s `finally`
completes shortly after `asyncio.run` tears the loop down, you can get
"Task was destroyed but it is pending!" warnings, and `_read_stdout`'s `finally`
(`:166-169`) may still try to `self._events.put(...)` after the queue's consumer
is gone.

**Fix:** await them with a guard:
```python
for task in (self._reader_task, self._stderr_task):
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
```
Do this *before* (or alongside) terminating the process so the readline futures
unwind cleanly.

### I8 — terminate-timeout path: `kill()` is sent but the zombie is never reaped
`claude_code.py:132-142`.

When `terminate()` + `wait_for(..., timeout=5)` times out, the `except` calls
`self._proc.kill()` but does not `await self._proc.wait()` afterward. The killed
child becomes a zombie until asyncio's transport eventually reaps it — not
guaranteed to happen before interpreter exit. Add a final
`await self._proc.wait()` (it returns immediately once SIGKILL lands) so the
child is reaped deterministically. Pair this with I7's awaited cancellation.

---

## Nits

### N9 — `Speaker._cancel` Event reuse is correct but racy under concurrent play
`audio.py:70,77,84,89`.

The `Event` is reused across calls and `play()` clears it at entry (`:77`). This
is fine for the half-duplex loop where exactly one `play()` runs at a time. It is
*not* safe if `play()` is ever called concurrently (a future barge-in re-arch):
call A's `clear()` at `:77` would erase a `stop()` aimed at it, and a `stop()`
landing in the window between `clear()` and the first `is_set()` check is lost.
The check at `:84` is also only between blocks — a `stop()` arriving while
`stream.write` is blocked in the thread (`:88`) isn't honored until that ~128 ms
block finishes (acceptable, matches the docstring's "within one block", but worth
noting it's not instantaneous). No fix needed today; flag for the barge-in work:
mint a fresh `Event` (or a generation token) per `play()` call.

### N10 — `OutputStream` cleanup on exception is correct; one gap
`audio.py:78-93`.

The `try/finally` correctly stops+closes the stream on any exception or cancel.
One gap: `sd.OutputStream(...)` and `.start()` (`:78-81`) are *outside* the
`try`. If `.start()` raises (device busy, sample-rate unsupported), the already
-constructed stream object is never closed. Move construction inside the `try`,
or wrap start in its own guard. Low-frequency, but it's a handle leak on the
audio device.

### N11 — mic callback drops frames silently while building the queue
`audio.py:32-37`.

`_callback` ignores the `status` arg, which PortAudio sets on input overflow
(dropped frames). During heavy load (whisper transcription on the same box) the
mic can overflow and you'd lose audio with no signal. Consider logging when
`status` is truthy under `cfg.debug`. Also: there's no bound on `self._queue`
(`audio.py:27`) — if the consumer stalls (a slow STT), frames accumulate
unboundedly in memory. A `maxsize` with a documented drop-oldest policy would be
more honest about real-time behavior.

### N12 — `set_muted(False)` is unconditional once `speaking` flips true, even on error exit
`orchestrator.py:110-112`.

After **B1** is fixed, double-check this: on a `BackendError` mid-turn where
`speaking` is `True`, the `finally`-less `if speaking:` block still unmutes the
mic and resets VAD before the run tears down. Harmless once the loop actually
exits, but if you instead choose to *recover* from `BackendError`, you'd want the
unmute to be in a `finally` so an exception between `break` and the unmute can't
strand the mic muted.

### N13 — sentence regex won't flush text that never ends in punctuation
`orchestrator.py:31,118-125`.

`_drain_sentences` only emits on `.!?`. Assistant prose without terminal
punctuation (code blocks, bullet fragments, a trailing clause) stays in
`pending` until `TurnComplete` flushes it (`:97-101`) — correct, but it means
such text is spoken in one late burst at end-of-turn rather than streaming. Fine
for now; note it if perceived latency on punctuation-light replies matters.

### N14 — `_consume_turn(events)` is untyped
`orchestrator.py:76`.

`events` is annotated `-> None` on the method but the param itself is untyped.
The project's CLAUDE.md calls for typed public APIs; annotate it
`events: AsyncIterator[AgentEvent]` for clarity (it's the long-lived stream from
`backend.events()`).

---

## What's solid (verified, no action)

- **Events generator reuse across turns** (`orchestrator.py:57,70,83` +
  `claude_code.py:116-119`). The generator is created once in `run()` and passed
  into each `_consume_turn`; the `async for ... break` *suspends* the generator
  at the `yield` (`:118-119`) without closing it (no `aclose()` anywhere —
  confirmed by grep), so the next turn resumes the same object and drains the
  same queue. Resumption is correct and leak-free. `tests/test_loop_offline.py`
  explicitly covers the two-turn case.
- **Double-speak avoidance** (`claude_code.py:226-236`). Text is emitted only
  from stream deltas; the full assistant message emits tool activity only. Given
  partials are enabled in `_argv` (`:75`), there is no double-speak. (See I4 for
  the missing-fallback caveat.)
- **`_turn_text` lifecycle** — cleared in `send()` (`:101`) and in the `result`
  handler (`:206`), so rollups don't bleed across turns.
- **Mute/unmute correctness** for the happy path — first speech of a turn mutes
  (`:90-92,98-100`), end-of-turn unmutes + resets VAD (`:110-112`); matches the
  `[True, False, True, False]` assertion in the offline test.
- **`session.py`** atomic write (mkstemp-in-same-dir + fsync + `os.replace`) and
  corrupt-store tolerance are textbook. No issues.

---

## Suggested priority order
1. **B1** — fatal-on-error loop (and the `_run_text` twin).
2. **I3 / I5** — temp-file leak + lost final utterance (both data/UX losses).
3. **I2 / I7 / I8** — loop-binding + task/process teardown hygiene.
4. **I4 / I6** — version-drift robustness (silent dead-air, fragile heuristic).
5. Nits as polish.
