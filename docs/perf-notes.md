# talk2me — Latency & Throughput Notes

Performance review of the half-duplex voice loop. Focus: **perceived voice
responsiveness** — the wall-clock from "user stops talking" to "first audio out
of the speaker," and gaps between spoken sentences. All findings are
file:line-anchored against the current tree. No code was changed.

## Critical-path budget (where the time actually goes)

For one turn, perceived latency = the sum of these serial stages (none overlap
today):

```
end-of-speech detected   (segment.py: silence_ms = 900ms trailing)
  -> whisper transcribe  (whisper.py: full-utterance, blocking)
  -> backend round-trip  (agent streams text)
  -> per sentence: say -o WAV  (say.py: full render before any audio)
  -> per sentence: open OutputStream + play  (audio.py)
```

The whole pipeline is **strictly sequential per sentence**: orchestrator.py:89-93
awaits `_speak` (which awaits `play`) before pulling the next sentence. So every
fixed cost below is paid once *per sentence*, not once per turn.

---

## Ranked findings

### P0 — `say` renders the ENTIRE sentence to a temp WAV before any audio plays

**File:** `talk2me/tts/say.py:29-56` (`SayTTS.synthesize` → `_render`)

`synthesize` calls `await asyncio.to_thread(self._render, text)` (say.py:33),
which runs `subprocess.run(["say", "-o", path, ...])` to completion
(say.py:55) and only *then* opens the WAV and starts yielding blocks
(say.py:34-36). The `AsyncIterator[np.ndarray]` contract in protocols.py:44-57
was designed for *streaming* synthesis ("lets the player start before synthesis
finishes"), but `say` defeats it: nothing is yielded until the full file exists.

**Cost.** `say -o file` is batch — it must synthesize the whole utterance, flush
the WAV, return, then we re-open and read it (say.py:58-64). For a typical
1–2 second spoken sentence this is roughly **300–700ms of dead air before the
first sample plays**, plus subprocess spawn (~20–50ms) and a temp-file write +
read round-trip on disk. This fixed cost repeats for **every sentence** because
of the serial loop in orchestrator.py:89-93. A 4-sentence answer pays it 4×.

**Why it dominates.** It is the single largest controllable contributor to
time-to-first-audio on every turn, and unlike whisper it scales with the number
of sentences in the reply.

**Recommendations (ranked):**
1. **Pipe `say` instead of file-hopping.** `say --data-format=LEI16@16000 -o -`
   (or stdout via a pipe) lets you read PCM as it is produced and yield blocks
   incrementally — restores the streaming contract, kills the temp-file write +
   read, and starts audio mid-synthesis. Biggest single win for first-audio
   latency; no new dependency.
2. **Pre-warm / overlap synthesis with playback** (see P4): start rendering
   sentence *N+1* while sentence *N* plays, so the per-sentence render cost is
   hidden behind playback for every sentence after the first.
3. **Swap to streaming neural TTS** for the real fix. KittenTTS
   (tts/kitten.py) is already wired but has the *same* render-whole-chunk flaw
   (kitten.py:48-54) — it is not actually streaming either. A true streaming
   engine (ElevenLabs streaming, or a model that yields frames) collapses
   time-to-first-audio to ~tens of ms.

**Expected impact:** removes ~300–700ms per sentence from time-to-first-audio;
multiplies across multi-sentence replies.

---

### P1 — `Speaker.play` opens a NEW `OutputStream` per sentence

**File:** `talk2me/audio.py:75-92` (`Speaker.play`)

Every call to `play` does `sd.OutputStream(...)` → `stream.start()`
(audio.py:78-81) and `stream.stop(); stream.close()` in `finally`
(audio.py:91-92). Since orchestrator.py:115 calls `play` once per sentence,
a fresh PortAudio output stream is opened and torn down for **each sentence**.

**Cost.** Opening a CoreAudio output stream is not free: device negotiation +
buffer allocation typically costs **~30–150ms of startup latency** and the
stream's initial buffer must prime before audio is audible. Worse for
*perceived* quality: tearing the stream down and reopening it between sentences
produces an **audible click/gap at every sentence boundary**, making a
multi-sentence reply sound choppy rather than continuous. The very first
`stream.write` after `start()` also tends to underrun/prime, adding jitter.

**Recommendations (ranked):**
1. **Reuse one `OutputStream` per turn.** Open it once when `speaking` first
   flips true (orchestrator.py:90-92 / 98-100) and close it in the
   `if speaking:` cleanup block (orchestrator.py:110-112). `play` becomes
   "write blocks to the already-running stream." Eliminates per-sentence startup
   and the inter-sentence click/gap.
2. **Or keep one persistent stream for the process** and just pause/resume.
   Simpler lifecycle, but holds the audio device open the whole session.
3. **Pre-warm the stream** before the first sentence is ready (open it the
   moment the turn starts, before TTS returns) so the first sentence doesn't
   eat the open cost on the critical path.

**Expected impact:** removes ~30–150ms per sentence and the audible
inter-sentence click; reply playback becomes continuous.

---

### P2 — Mic `asyncio.Queue` is unbounded → backlog during muted think-windows

**File:** `talk2me/audio.py:27` (`self._queue: asyncio.Queue = asyncio.Queue()`),
fed by `_callback` at audio.py:32-37.

The queue has no `maxsize`. The mic `InputStream` callback runs on PortAudio's
audio thread and pushes every frame via `call_soon_threadsafe(put_nowait, ...)`
(audio.py:37). Consumption happens only in `segment_utterances` pulling
`self.mic.frames()` (orchestrator.py:61-63 → audio.py:58-60).

**The hazard.** During an agent turn the loop is busy in `_consume_turn`
(orchestrator.py:76-112) — transcribing, awaiting backend deltas, and
**`await`-ing `_speak` per sentence**. While speaking, the mic is *muted*
(`set_muted(True)`, orchestrator.py:91) so `_callback` early-returns
(audio.py:33-34) and no frames enqueue — good, that path is safe. **But:**

- Between `mic.start()` (orchestrator.py:56) and the first frame consumption,
  and during transcribe + backend-wait *before* `speaking` flips true, the mic
  is **live and unconsumed**. `transcribe` (orchestrator.py:64) and the early
  part of `_consume_turn` run while frames keep arriving with no reader. At
  16 kHz / 30 ms frames that's ~33 frames/sec piling onto an unbounded queue.
- Each backlogged frame is a `.copy()` of the block (audio.py:36) — unbounded
  memory growth under a long backend turn, and a **stale-audio backlog** the
  next `segment_utterances` pass must chew through before it sees live speech.
  Stale frames also feed the VAD/segmenter, so the *next* turn can mis-segment
  on audio captured seconds ago.

**Recommendations (ranked):**
1. **Bound the queue** (`asyncio.Queue(maxsize=N)`) and drop-oldest on overflow
   in `_callback` (it's already on a non-loop thread; do a non-blocking
   discard rather than `put_nowait` raising). Caps memory and guarantees the
   consumer always sees *recent* audio.
2. **Drain/flush the queue when un-muting** (orchestrator.py:111) so the next
   listen window starts from "now," not from a backlog accumulated during
   transcribe/think. Prevents stale-audio mis-segmentation.
3. **Mute earlier** — set `set_muted(True)` for the whole think-window (right
   after `backend.send`, orchestrator.py:69) instead of waiting for the first
   spoken sentence, so no frames accumulate during transcribe + backend wait.
   (Trade-off: forecloses barge-in during thinking, which half-duplex already
   doesn't support.)

**Expected impact:** bounds memory, eliminates stale-audio backlog that delays
or corrupts the *next* turn's segmentation.

---

### P3 — Whisper model size + `int8` + transcribe-in-thread: first-token cost

**File:** `talk2me/stt/whisper.py:18-59`

Defaults: `model="base.en"`, `compute_type="int8"` (whisper.py:21-24,
config.py:31). Model load is lazy on first transcribe (whisper.py:32-41), and
`transcribe` runs blocking C++ in a thread (whisper.py:43-44) — correct, keeps
the loop breathing.

**Costs / tradeoffs:**
- **First-utterance penalty.** `_ensure_model` (whisper.py:32-41) loads
  faster-whisper on the *first* transcribe, on the critical path of turn 1. For
  `base.en` int8 that's a multi-hundred-ms-to-second one-time hit the user
  feels as a slow first reply.
- **`int8` is the right default** on CPU/Apple — it's the fastest compute_type
  and quality loss on `base.en` is minor for command-style speech. Keep it.
- **`base.en` is a reasonable middle.** `tiny.en` would cut transcribe latency
  ~2–3× (often <150ms for a short utterance vs ~300–500ms for base) at a real
  accuracy cost on names/jargon. `small.en` roughly doubles latency for
  marginal gain on clean mic input.
- **`vad_filter=False`** (whisper.py:57) is correct — segmentation already
  happened upstream (segment.py); re-running whisper's internal VAD would add
  cost and risk clipping.
- **Note:** `config.vocab` (config.py:32) is collected but never wired into
  `WhisperSTT.initial_prompt` (whisper.py:29, 56). Biasing names/jargon via
  `initial_prompt` is free accuracy with no latency cost — currently unused.

**Recommendations (ranked):**
1. **Pre-load the model at startup**, not on first transcribe — call
   `_ensure_model()` during `Orchestrator.run` setup (alongside
   orchestrator.py:55-56) so turn 1 isn't penalized. Easy, high-value.
2. **Expose model size as the latency dial.** For responsiveness-first use,
   default `tiny.en`; keep `base.en` for accuracy-first. Document the tradeoff.
3. **Wire `cfg.vocab` into `initial_prompt`** to recover the accuracy `tiny.en`
   loses — free, no latency.
4. Keep `int8` and `vad_filter=False`.

**Expected impact:** removes the first-turn model-load stall from the critical
path; optional `tiny.en` cuts steady-state transcribe latency ~2–3×.

---

### P4 — Sentence-chunk granularity vs first-audio latency in `_drain_sentences`

**File:** `talk2me/orchestrator.py:83-93, 118-125` + regex
orchestrator.py:31 (`_SENTENCE`).

`_drain_sentences` only emits a chunk once a full sentence terminator
(`[.!?]+`) is seen (orchestrator.py:31, 122-124). The loop then speaks each
ready sentence **inline and serially** (orchestrator.py:89-93, awaiting
`_speak`).

**Two coupled latency effects:**
1. **First-audio waits for the first full sentence.** No audio plays until the
   backend has streamed a complete sentence *and* it's been rendered (P0) and a
   stream opened (P1). For a long opening sentence, time-to-first-audio is
   gated by the slowest of "model finishes sentence 1" vs the TTS render.
2. **Serial speak blocks text consumption.** Because line 93 `await`s `_speak`,
   while sentence 1 is rendering+playing, the loop is **not** reading new
   `AssistantTextDelta` events. Backend text buffers (fine for correctness) but
   sentence 2 can't even *start* rendering until sentence 1 fully finishes
   playing → the P0/P1 fixed costs are paid end-to-end, never overlapped.

**Recommendations (ranked):**
1. **Decouple speak from text consumption with a queue.** Push ready sentences
   onto an `asyncio.Queue`; a separate consumer task renders+plays them. This
   lets sentence N+1 render while sentence N plays — the single highest-leverage
   structural change, and it's what makes the P0/P1 pre-warm fixes actually
   overlap instead of just shrinking serial costs.
2. **Clause-level chunking for the *first* chunk only.** Flush on the first
   comma/clause boundary (or after ~N words) for the opening fragment so audio
   starts sooner, then fall back to sentence chunking. Trims perceived latency
   at the very start of a reply where it matters most.
3. **Guard against pathological long sentences** — if `pending` exceeds a word
   threshold with no terminator, flush a clause so a run-on doesn't stall audio
   indefinitely.

**Expected impact:** with the queue (rec 1), per-sentence render+open costs from
P0/P1 are hidden behind playback for all sentences after the first; clause-first
chunking cuts opening-fragment latency by hundreds of ms.

---

## Suggested fix order (highest leverage first)

1. **P4 rec 1** — sentence queue / decouple speak from consumption. Unlocks
   overlap; everything else compounds off it.
2. **P0 rec 1** — pipe `say` to stdout instead of temp WAV. Kills the biggest
   per-sentence fixed cost.
3. **P1 rec 1** — reuse one `OutputStream` per turn. Kills per-sentence stream
   open + the audible inter-sentence click.
4. **P3 rec 1** — pre-load whisper at startup. Removes the first-turn stall.
5. **P2 recs 1–2** — bound + flush the mic queue. Correctness/robustness for the
   *next* turn under long think-windows.
6. **P3 recs 2–3** — `tiny.en` option + wire `cfg.vocab`. Tunable accuracy/speed.

Items 1–3 together attack time-to-first-audio AND inter-sentence gaps — the two
things the user actually hears.
