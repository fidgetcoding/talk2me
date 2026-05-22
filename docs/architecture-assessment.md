# talk2me — Architecture Assessment

Read-only design review. Scope: `talk2me/*` + `tests/*`. No code was changed; this
document is the only write. All citations are `file:line`.

Verdict up front: the clean-swap thesis ("orchestrator depends only on Protocols")
is **substantially achieved** — the dependency graph is acyclic and inversion is real.
But the abstraction has three concrete leaks (sample-rate, concrete `Mic`/`Speaker`,
and an unenforced VAD/Mic frame-size contract), the runtime `Protocol`s are
necessary-but-not-sufficient as written, and a streaming-STT or WebRTC transport would
break the current contract at the *segmentation boundary*, not at the provider boundary.
Details and surgical, contract-preserving refactors follow.

---

## 1. Protocol adherence

### 1a. Does every provider satisfy the contract?

| Protocol | Members (`protocols.py`) | Providers | Satisfies? |
|---|---|---|---|
| `VAD` (`protocols.py:19`) | `sample_rate:int`, `frame_samples:int`, `is_speech`, `reset` | `EnergyVAD` (`vad/energy.py:12`), `SileroVAD` (`vad/silero.py:57`) | Yes — both set both attrs in `__init__` and define both methods. |
| `STT` (`protocols.py:35`) | `async transcribe(audio, sample_rate) -> str` | `WhisperSTT` (`stt/whisper.py:43`) | Yes. |
| `TTS` (`protocols.py:44`) | `sample_rate:int`, `async synthesize(text) -> AsyncIterator` | `SayTTS` (`tts/say.py:23`), `KittenTTS` (`tts/kitten.py:31`), `NullTTS` (`tts/null.py:16`) | Yes structurally. See note on `KittenTTS` below. |
| `AgentBackend` (`protocols.py:61`) | `start`, `send`, `events`, `interrupt`, `close` | `ClaudeCodeBackend` (`backends/claude_code.py:36`) | Yes — all five present. |

One real divergence in spirit, not in signature:

- **`KittenTTS` loads eagerly in `__init__`** (`tts/kitten.py:36`, `_load_model()` is
  called from the constructor) while every other heavy provider is lazy
  (`WhisperSTT._ensure_model` at `stt/whisper.py:32`; `SileroVAD._load` at
  `vad/silero.py:127`). `test_factory.py:107` asserts "construct without side effects"
  for STT and backend; an equivalent assertion for `KittenTTS` would fail because
  constructing it imports `kittentts` and instantiates a model. This is a **latent
  contract violation of the project's own "construction is cheap/lazy" convention**,
  not of the typed `TTS` Protocol. The Protocol does not encode laziness, which is
  exactly why it slips through (see 1b).

### 1b. Are `runtime_checkable` Protocols enough, or are ABCs needed?

`runtime_checkable` here buys very little and quietly mis-leads. Three facts:

1. **`runtime_checkable` only checks member *names*, never signatures or types.** The
   `isinstance(x, TTS)` assertions in `test_factory.py:54,73,91,113` will pass for any
   object that merely *has* attributes named `sample_rate` and `synthesize` — a
   `synthesize` that is sync, takes wrong args, or returns a string would still pass
   `isinstance`. So the runtime checks give a false sense of enforcement.

2. **Attribute-bearing Protocols (`VAD`, `TTS`) are especially weak under
   `runtime_checkable`.** `is_speech`/`reset`/`synthesize` are checked as present
   methods, but `sample_rate`/`frame_samples` are only verified to *exist as
   attributes* — and only after `__init__` has run. A class that declares them but
   forgets to assign passes the class-level check and fails at use.

3. **Nothing in the runtime path actually calls `isinstance` against a Protocol.**
   The orchestrator never does (`orchestrator.py` takes the providers as already-typed
   constructor args). Only the *tests* call `isinstance`. So `runtime_checkable` is
   carrying test-only weight that static typing (mypy/pyright on the `factory.py`
   return annotations `-> VAD`/`-> STT`/`-> TTS`/`-> AgentBackend`) already carries
   better.

**Do they need ABCs?** No — and switching to ABCs would be a net regression for this
design. The whole value proposition (`protocols.py:3`, `factory.py:4`) is *structural*
typing: a provider satisfies the contract without importing or inheriting from
talk2me. ABCs force an `import` + `class X(VADBase)` coupling that defeats "write a new
class that satisfies the contract — no orchestrator changes." Recommendation:

- **Keep `Protocol`. Drop `runtime_checkable` reliance for correctness;** treat the
  `isinstance` test assertions as smoke only, and lean on **static** type checking as
  the real gate. There is currently **no mypy/pyright step in CI** (`.github/workflows/ci.yml`
  runs only the four runtime scripts) even though the package ships `py.typed`
  (`pyproject.toml:26`) advertising itself as typed. That is the single highest-value
  protocol-enforcement gap: add a `mypy talk2me` (or `pyright`) job. Static checking is
  what would actually catch a sync `synthesize` or a wrong `transcribe` signature —
  the thing `runtime_checkable` pretends to do but does not.
- If a runtime guarantee is genuinely wanted at the factory boundary, a tiny explicit
  assertion (e.g. `assert inspect.isasyncgenfunction(tts.synthesize)`) buys more than
  `isinstance(tts, TTS)`.

---

## 2. Coupling / dependency graph

### 2a. Does the orchestrator depend only on Protocols? — Verified, with one caveat.

`orchestrator.py` imports: `audio.Mic, Speaker` (`:19`), `config.Config` (`:20`),
`events.*` (`:21`), `protocols.STT,TTS,VAD,AgentBackend` (`:28`), `segment.segment_utterances`
(`:29`). It imports **no** concrete provider (`WhisperSTT`, `SayTTS`, `EnergyVAD`,
`ClaudeCodeBackend` never appear). The four swappable axes are injected as Protocol
types in `__init__` (`orchestrator.py:35-52`). That half of the thesis holds cleanly.

**Caveat — `Mic`/`Speaker` are concrete, not Protocols** (`orchestrator.py:19`,
constructor params `mic: Mic, speaker: Speaker` at `:50-51`). The two audio-hardware
edges are the *only* concrete couplings the orchestrator carries. This is why
`tests/fakes.py` can supply `FakeMic`/`FakeSpeaker` (`fakes.py:19,43`) **only by duck
typing** — they are not declared to satisfy any contract, so there is no type checker
or `isinstance` confirming the fakes match the real shape. The loop test works
(`test_loop_offline.py`) but the audio I/O surface is the one un-inverted dependency.
See refactor R1.

### 2b. Where the abstraction leaks

**Leak 1 — `sample_rate` is asserted, not negotiated; 16 kHz is hardcoded in two
providers.**

- `WhisperSTT._transcribe_sync` hardcodes the model's required rate as a literal
  `16000` and resamples anything else (`stt/whisper.py:50-51`, `_resample_to_16k` at
  `:62`). The `STT.transcribe` Protocol *does* take `sample_rate` (`protocols.py:38`),
  so this leak is contained — but the magic number `16000` is duplicated rather than
  named.
- `SayTTS.sample_rate = 16000` is a hardcoded class attribute (`tts/say.py:23`) and the
  `say` invocation pins `--data-format=LEI16@16000` (`tts/say.py:46`). `NullTTS` also
  hardcodes `16000` (`tts/null.py:16`). `KittenTTS` correctly advertises `24000`
  (`tts/kitten.py:31`).
- The plumbing that makes this *mostly* fine: `__main__._run_voice` builds the
  `Speaker` from `tts.sample_rate` (`__main__.py:79`), not from `cfg.sample_rate`. So
  a 24 kHz `KittenTTS` would drive a 24 kHz `Speaker` correctly — the output path
  honors the provider's rate. Good.
- **The actual leak is on the *input* side.** `Config.sample_rate = 16000`
  (`config.py:23`) is the mic/STT rate, and `Mic` is built with `cfg.sample_rate`
  (`__main__.py:78`) while `WhisperSTT` *assumes* its input is already 16 kHz
  ("We capture at 16 kHz, so no resample needed" — `stt/whisper.py:48-49`). If a user
  set `cfg.sample_rate` to anything else, the mic would capture at that rate, the
  orchestrator would pass `self.mic.sample_rate` into `transcribe`
  (`orchestrator.py:64`), and whisper's guard would resample — so it is *technically*
  correct, but the comment lies and the coupling is implicit. The VAD also silently
  assumes its `sample_rate` equals the mic's; nothing enforces `vad.sample_rate ==
  mic.sample_rate` (segmentation derives `frame_ms` from `vad.frame_samples /
  vad.sample_rate` at `segment.py:27`, independent of the mic).

**Leak 2 — the VAD/Mic frame-size contract is documented but unenforced.**

- `factory.frame_samples` computes the shared frame size and the comment says "Mic and
  VAD MUST agree on this" (`factory.py:16`). `__main__` wires both from the same call
  (`Mic(cfg.sample_rate, factory.frame_samples(cfg))` at `:78`; VAD via
  `factory.build_vad` which also calls `frame_samples` at `factory.py:26`). So in the
  real wiring they agree **by construction discipline, not by type**. The `VAD`
  Protocol exposes `frame_samples` (`protocols.py:22`) but `Mic` does **not** expose a
  matching attribute the orchestrator can assert against, and `segment_utterances`
  trusts that incoming frames are exactly `vad.frame_samples` long
  (`segment.py:24-25` docstring) without checking. A mismatch would not raise — it
  would silently corrupt segmentation (`EnergyVAD.is_speech` would compute RMS over a
  wrong-length frame; `SileroVAD` buffers and would still run, masking the bug).
- `Config.frame_ms` (`config.py:24`) is the source of truth for frame size but lives in
  config while the derivation lives in factory — a reader has to cross two files to
  see the contract.

**Leak 3 — concrete `Mic`/`Speaker` in `orchestrator` and `__main__` (restated as
coupling).** Covered in 2a. `__main__._run_voice` instantiates `Mic`/`Speaker`
directly (`__main__.py:78-79`) rather than through a factory builder like every other
provider. So the "one place that maps config to concrete classes" (`factory.py:1-2`)
is incomplete: VAD/STT/TTS/backend go through `factory`, but audio I/O is hand-wired in
`__main__`. Adding a non-sounddevice audio path (e.g. WebRTC, a network mic) means
editing `__main__`, contradicting the "no CLI change to swap a provider" promise
(`factory.py:5-6`).

**Leak 4 (minor) — `_consume_turn(events)` is untyped.** `orchestrator.py:76` declares
`async def _consume_turn(self, events) -> None` with no annotation on `events`. It is
the backend's `AsyncIterator[AgentEvent]`. Internal method, but it is the one place the
orchestrator's event consumption is typed by inference only. See section 3.

**Non-leak worth crediting:** the event layer (`events.py`) is a clean closed union
(`AgentEvent` at `events.py:51`) and `ClaudeCodeBackend._translate` is the only place
stream-json shapes are known (`backends/claude_code.py:187`). The orchestrator pattern-
matches on the union (`orchestrator.py:84-108`) and never sees raw JSON. This is the
strongest part of the inversion — swapping the agent runtime is genuinely one file.

---

## 3. Compliance (project rules: files <500 lines, typed public APIs, input validation at boundaries)

### Files < 500 lines — PASS.
Largest source file is `backends/claude_code.py` at 236 lines; `vad/silero.py` 221;
`session.py` 217. All comfortably under 500. No violation.

### Typed public APIs — mostly PASS, specific gaps:

- **`orchestrator.py:76`** — `_consume_turn(self, events)`: `events` param has no type
  annotation. Should be `AsyncIterator[AgentEvent]`. (Private method, but the
  orchestrator is the core; flag.)
- **`audio.py:32`** — `Mic._callback(self, indata, frames, time_info, status)`: four
  untyped params (the file even carries `# noqa: ANN001`). This is a sounddevice
  callback signature; acceptable but explicitly opts out of the typed-API rule.
- **`audio.py:58`** — `Mic.frames(self)` has no return annotation; it is an async
  generator yielding `np.ndarray`. The `STT`/`VAD`/`segment` paths all expect
  `AsyncIterator[np.ndarray]`; `Mic.frames` is a **public** API consumed by
  `orchestrator.run` (`orchestrator.py:62`) and should be annotated
  `-> AsyncIterator[np.ndarray]`.
- **`audio.py:75`** — `Speaker.play(self, blocks) -> bool`: `blocks` untyped (it is an
  `AsyncIterator[np.ndarray]`). Public API consumed by `orchestrator._speak`
  (`orchestrator.py:115`). Flag.
- **`audio.py:32` / `:75`** — these are the same surface as the un-inverted `Mic`/`Speaker`
  coupling; an `AudioSource`/`AudioSink` Protocol (R1) would force the annotations.
- **`tts/kitten.py:38`** — `_load_model(self) -> object` returns `object`, then
  `self._model.generate(...)` is called on it (`tts/kitten.py:61`). Returning `object`
  defeats type checking on the one call that matters; a `Protocol` or `Any` with a
  comment would be more honest, though this is a private attr.
- Everywhere else public signatures are fully typed (`protocols.py`, `config.py`,
  `events.py`, `factory.py`, `session.py`, `segment.py`, `stt/whisper.py`,
  `backends/claude_code.py` public methods). Good baseline.
- **Process gap:** `py.typed` is shipped (`pyproject.toml:26`) but **no static type
  check runs in CI** (`.github/workflows/ci.yml` has no mypy/pyright step). A package
  that advertises `py.typed` without a type-check gate can drift; this is the
  compliance hole behind every annotation gap above.

### Input validation at boundaries — UNEVEN.

Strong where present:
- **`session.py`** is exemplary: `save_session` validates UUID shape and type
  (`session.py:114`), writes atomically via temp-file + `os.replace` (`session.py:130-139`),
  `load_last_session` is total (never raises; returns `None` on missing/corrupt/bad-shape —
  `session.py:160-177`). Path is `.resolve()`d (`session.py:89`) and `$TALK2ME_HOME` is
  honored safely (`session.py:80`). This boundary is hardened. (Note: `session.py` is
  currently **dead code** — see section 4 / R4. It documents wiring that does not exist
  in `__main__.py` or `claude_code.py`.)
- **`SileroVAD.__init__`** validates `sample_rate`, `frame_samples`, `threshold` ranges
  (`vad/silero.py:72-77`). Good.
- **`ClaudeCodeBackend._read_stdout`** treats stream-json defensively: non-JSON lines
  skipped (`backends/claude_code.py:158`), unknown shapes ignored
  (`_translate` returns `[]`, `:209`), reader never dies silently (`:164-165`). This is
  the most important boundary (untrusted subprocess output) and it is well-guarded.

Gaps:
- **`__main__._collect_vocab` (`__main__.py:58-64`)** opens a user-supplied
  `--vocab-file` path with no validation, no size cap, and no error handling — a
  missing file raises an unhandled `FileNotFoundError` straight out of `main()`. This
  is a CLI boundary; should fail with a clean message, not a traceback. (Path-traversal
  is not a real risk here — it is the user's own shell — but the bare `open` is a rough
  edge.)
- **`EnergyVAD.is_speech` (`vad/energy.py:26`)** only guards `frame.size == 0`. It does
  not check `frame.size == self.frame_samples` — a wrong-length frame produces a wrong
  RMS silently (Leak 2). The VAD is a boundary between raw audio and turn logic; a
  length assertion belongs here or in `segment_utterances`.
- **`segment_utterances` (`segment.py:18`)** trusts frame length and `vad.frame_samples
  > 0` (division at `segment.py:27`); a zero would `ZeroDivisionError`. `SileroVAD`
  guards this at construction but `EnergyVAD` does not (`vad/energy.py` accepts any
  `frame_samples`).
- **`SayTTS._render` (`tts/say.py:43`)** passes user/agent text to `subprocess.run(["say",
  ..., "--", text])`. The `--` guard is correct and prevents flag injection — credit —
  but there is no length cap; a very long assistant sentence becomes one `say` call.
  Low risk, noted.

---

## 4. Extensibility — what would BREAK the clean-swap design

The design swaps cleanly along the **four declared axes** (VAD/STT/TTS/backend) *as long
as the new provider keeps the same interaction shape*. The shape, not the type, is the
fragile part. Two concrete futures:

### 4a. Streaming STT (Deepgram, AssemblyAI realtime) — **BREAKS the contract.**

The `STT` Protocol is **batch-only**: `transcribe(audio, sample_rate) -> str` takes a
*complete, already-segmented utterance* and returns a *final* string
(`protocols.py:38`). The orchestrator enforces this temporally:
`segment_utterances` runs to completion first (`orchestrator.py:61-63`), *then* calls
`transcribe` on the whole buffer (`orchestrator.py:64`). A streaming STT inverts that —
it wants raw frames *as they arrive* and emits partial then final transcripts, and it
typically does its own VAD/endpointing. To add one you would have to:

- bypass or gut `segment_utterances` (the VAD axis becomes redundant or conflicting),
- change the orchestrator's `listen → segment → transcribe → send` ordering
  (`orchestrator.py:61-69`), and
- add partial-transcript events the loop doesn't model.

So the leak is architectural: **VAD + segmentation + batch-STT are three coupled
assumptions baked into the orchestrator's main loop, not behind a single Protocol.** A
streaming STT is not a drop-in. This is the biggest extensibility risk and it
contradicts the "Deepgram is a new class plus one factory branch" claim in the README
(`README.md:15`). Deepgram *batch* mode fits; Deepgram *streaming* does not.

### 4b. WebRTC transport / Pipecat wrap — **BREAKS at the audio I/O edge, not the providers.**

The spec's "Pipecat as a possible future wrap" would replace the *transport*: instead of
local `sounddevice` mic/speaker, audio comes/goes over a WebRTC peer. What breaks:

- `Mic`/`Speaker` are concrete and hand-wired in `__main__` (`__main__.py:78-79`), not
  behind a Protocol or a factory builder (Leak 3 / 2a). A WebRTC source/sink cannot be
  injected the way a VAD can; you must edit the orchestrator's constructor types
  (`orchestrator.py:50-51`) and `__main__`.
- `audio.py` ties playback to a *pull* model (`Speaker.play` consumes an async iterable
  of blocks, `audio.py:75`) and capture to an asyncio queue fed by a sounddevice
  callback (`audio.py:32-37`). Pipecat/WebRTC is frame-pushed and has its own
  buffering, jitter, and turn model — the half-duplex mute logic
  (`orchestrator.py:91,99,111` calling `self.mic.set_muted`) assumes local hardware
  echo control that a network transport handles differently.
- Pipecat owns the pipeline itself (VAD→STT→LLM→TTS), so wrapping talk2me inside it
  means talk2me's orchestrator and Pipecat's pipeline both want to be the conductor —
  you would wrap at the *backend* level (`AgentBackend`) and discard talk2me's audio +
  segmentation layers entirely. That is a fork of the loop, not a swap.

**Net:** the four-axis swap story is true within the local, half-duplex, batch-STT
envelope it was designed for. Step outside that envelope (streaming recognition or a
non-local transport) and the orchestrator's main loop — not any provider — is what you
rewrite.

---

## 5. Concrete refactors that PRESERVE the contract

Ordered by leverage. None changes provider semantics; all keep structural typing.

**R1 — Promote `Mic`/`Speaker` to Protocols (`AudioSource`/`AudioSink`).** Add to
`protocols.py`:
- `AudioSource`: `sample_rate:int`, `frame_samples:int`, `start()`, `stop()`,
  `set_muted(bool)`, `frames() -> AsyncIterator[np.ndarray]`.
- `AudioSink`: `sample_rate:int`, `stop()`, `play(blocks) -> bool`.
Then type `orchestrator.__init__` against them (`orchestrator.py:50-51`) and add a
`factory.build_mic`/`build_speaker` so `__main__` stops hand-wiring (`__main__.py:78-79`).
Payoff: closes Leak 3, makes `FakeMic`/`FakeSpeaker` (`fakes.py:19,43`) checkable, and
makes a WebRTC transport (4b) injectable instead of a `__main__` edit. Exposing
`frame_samples` on `AudioSource` also lets the orchestrator assert
`mic.frame_samples == vad.frame_samples` (closes Leak 2).

**R2 — Name the magic rates; add one frame-size assertion.** Replace the literal
`16000` in `stt/whisper.py:50,62`, `tts/say.py:23,46`, `tts/null.py:16` with a named
constant (e.g. `WHISPER_RATE = 16000` local to whisper; keep `say` pinned to its own
constant). In `segment_utterances` (`segment.py`) or `Orchestrator.run`
(`orchestrator.py:61`), add `assert vad.frame_samples > 0` and, once R1 lands,
`assert mic.frame_samples == vad.frame_samples`. Fixes the lying comment at
`stt/whisper.py:48-49` and Leak 1/2 without changing any provider's behavior.

**R3 — Add a static type-check + lint job to CI.** `.github/workflows/ci.yml` runs only
runtime scripts. Add `mypy talk2me` (or `pyright`) and a `ruff` pass. This is what
actually enforces the `py.typed` claim (`pyproject.toml:26`) and the typed-public-API
rule, and it is what makes `runtime_checkable` redundant-in-a-good-way (section 1b). It
would immediately surface the missing annotations in `audio.py:32,58,75` and
`orchestrator.py:76`. Pure addition, no code semantics touched.

**R4 — Decide `session.py`'s fate (it is currently dead code).** `session.py` is a
complete, well-tested storage layer (`__all__` at `:59`) whose own docstring documents
wiring into `__main__.py` and `claude_code.py` that **does not exist** (no `--resume`
flag in `_parse_args`, no `save_session` call on `SessionReady`). Either (a) wire it —
the `SessionReady.session_id` join key is already emitted (`events.py:16`,
`claude_code.py:192`) so the hook is a 3-line add in the stdout reader or the
orchestrator's `SessionReady` branch (`orchestrator.py:104`) — or (b) move it under a
clearly-marked `experimental`/`unused` path so it does not read as live. Contract-
preserving either way; right now it is the one module that claims a capability the app
does not have.

**R5 — Annotate `_consume_turn` and harden the two CLI/audio boundaries.**
- `orchestrator.py:76`: `events: AsyncIterator[AgentEvent]`.
- `__main__._collect_vocab` (`__main__.py:58`): wrap the `open` with a clean error
  (file-missing → `argparse` error or a printed message + exit), and a sane size cap.
- `vad/energy.py`: mirror `SileroVAD`'s constructor validation (`vad/silero.py:72-77`)
  for `frame_samples > 0` / `sample_rate > 0` so both VADs validate identically at the
  boundary.

**R6 — (Optional, only if streaming STT is on the roadmap) introduce a `StreamingSTT`
second Protocol** rather than overloading `STT`. Keep the batch `STT` as-is; add a
parallel contract that consumes `AsyncIterator[np.ndarray]` and yields partial/final
transcript events, and let the orchestrator pick a *loop strategy* based on which it was
given. This contains the 4a breakage to a strategy branch in `Orchestrator.run` instead
of forcing every batch STT to pretend to stream. Larger change — flagged as the
principled fix, not a quick one.

---

## Appendix — file inventory and line counts (compliance evidence for §3)

```
talk2me/backends/claude_code.py  236   talk2me/stt/whisper.py        69
talk2me/vad/silero.py            221   talk2me/segment.py            70
talk2me/session.py               217   talk2me/factory.py            75
talk2me/__main__.py              135   talk2me/protocols.py          82
talk2me/orchestrator.py          125   talk2me/audio.py              92
talk2me/config.py                 47   talk2me/events.py             53
talk2me/tts/say.py                64   talk2me/tts/kitten.py         63
talk2me/tts/null.py               20   talk2me/vad/energy.py         34
tests/test_backend_translate.py  145   tests/test_factory.py        160
tests/fakes.py                   114   tests/test_sentences.py       74
tests/test_loop_offline.py        70   tests/manual_backend_check.py 67
tests/test_segment.py             55
```
All source files < 500 lines (max 236). No file-size violations.
