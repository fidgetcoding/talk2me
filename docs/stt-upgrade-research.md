# talk2me STT Upgrade: How to Approach Wispr Flow Quality Locally

*Research report, 2026-07-18. Rec #1 (hotwords + decode pinning + live context seeding) shipped same day; #2 and #3 open.*

## The core reframe (read this first)

Wispr Flow's quality is **~50% ASR, ~50% an LLM formatting/cleanup layer** — it says so itself: context-conditioned ASR then a fine-tuned Llama pass for "token-level formatting control," targeting <200ms ASR + <200ms LLM + <200ms network ([Wispr technical post](https://wisprflow.ai/post/technical-challenges), [Baseten case study](https://www.baseten.co/resources/customers/wispr-flow/)). Most of what "feels good" (filler removal, punctuation, list formatting, prose polish) is the LLM layer, not the acoustic model.

**But talk2me's consumer is a coding agent, not a text box.** That inverts the value of Wispr's tricks:
- **Punctuation / filler removal / prose formatting** — Wispr's headline features — are **low value** here. Claude Code reads unpunctuated, um-laden text fine.
- **Proper-noun / command / filename fidelity** is **the** thing that matters. "Refactor `useAuthStore`" mis-heard as "refactor use auth store" derails the agent. This is exactly where a small base.en model is weakest.
- **Self-correction** ("no wait, make that X") matters because the agent will otherwise *act* on the wrong instruction.

So the ranking below is tuned to talk2me's real failure surface (term accuracy + corrections), not Wispr's document-dictation surface.

---

## Ranked recommendations (by quality-per-effort)

### #1 — Fix contextual biasing on the existing faster-whisper path (ship today, ~zero deps)

**What.** `WhisperSTT` leaves the cheapest levers unused. Three changes in `talk2me/stt/whisper.py`:
1. **Switch `initial_prompt` → `hotwords`.** `initial_prompt` only conditions the *first segment* and is phrased as a sentence; `hotwords` biases the *whole* utterance and is purpose-built for proper nouns/technical terms. If both set, `initial_prompt` overrides `hotwords` — so it's a swap ([faster-whisper options](https://deepwiki.com/SYSTRAN/faster-whisper/4.3-transcription-options-and-configuration), [transcribe.py](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py)).
2. **Pin decode params:** `temperature=0.0`, `beam_size=5` (faster-whisper default; don't drop to 1), keep `condition_on_previous_text=False` for independent utterances.
3. **Feed live conversation context** — seed hotwords with proper nouns/filenames/symbols from the agent's last turn (what the user is about to say back). Free poor-man's "context awareness."

**Evidence.** Hotwords is the documented fix for exactly this failure mode (e.g. `FastAPI,PyTorch,Kubernetes`); explicit language + `temperature=0` beats sampling for dictation consistency ([saytowords](https://www.saytowords.com/blogs/Whisper-Best-Settings/), [faster-whisper guide](https://localaimaster.com/blog/faster-whisper-guide)).

**Tradeoff.** Latency-neutral. Ceiling-limited — base.en is still base.en; biasing helps known terms, not general acoustics. **Dep cost: zero.**

### #2 — Add a Parakeet-MLX STT engine (highest quality ceiling, local, no torch)

**What.** New `ParakeetMLXSTT` class implementing the `STT` Protocol, selected via `cfg.stt == "parakeet"`. Runs NVIDIA Parakeet TDT 0.6B on the M-series **GPU via MLX** — which sidesteps faster-whisper's biggest Mac weakness: **CTranslate2 has no Metal/CoreML backend, so faster-whisper is CPU-only.** Parakeet-MLX uses the GPU, so it's faster *and* more accurate than any local Whisper size.

**Evidence.**
- Parakeet v3 **~10× faster than Whisper large-v3-turbo for English AND more accurate** — 600M params vs Whisper large's 1.55B, leads on both WER and speed on the open ASR leaderboard ([Whisper Notes](https://whispernotes.app/blog/parakeet-v3-default-mac-model), [Spokenly](https://spokenly.app/blog/parakeet-vs-whisper)).
- On Apple Silicon, parakeet-mlx ~0.50s/sample vs ~1.02s mlx-whisper turbo (~2× faster than even GPU Whisper) ([Parakeety](https://www.parakeety.com/resources/parakeet-vs-whisper)).
- **No torch, no NeMo.** PyPI deps are MLX + numpy + librosa + audiofile/audresample + dacite — pure MLX, Python ≥3.10, Apple Silicon ([PyPI](https://pypi.org/project/parakeet-mlx/), [repo](https://github.com/senstella/parakeet-mlx)). Preserves talk2me's no-torch stance.
- VoiceInk confirms Parakeet as the fast local path — it ships Parakeet via **FluidAudio** (CoreML/ANE) ([VoiceInk README](https://github.com/Beingpax/VoiceInk/blob/main/README.md), [FluidAudio](https://github.com/FluidInference/FluidAudio)).

**Impl sketch.**
```python
# talk2me/stt/parakeet.py — satisfies STT Protocol
class ParakeetMLXSTT:
    def __init__(self, *, model="mlx-community/parakeet-tdt-0.6b-v2"): ...
    def _ensure_model(self):
        from parakeet_mlx import from_pretrained
        self._model = self._model or from_pretrained(self._model_name)
    async def transcribe(self, audio, sample_rate) -> str:
        return await asyncio.to_thread(self._transcribe_sync, audio, sample_rate)
```
Factory: add `cfg.stt == "parakeet"` branch in `build_stt`. Config: add `parakeet_model`. Model `parakeet-tdt-0.6b-v2` is English-only (ideal); `v3` adds 25 European langs.

**API friction to flag (verify before coding):** parakeet-mlx's high-level `transcribe()` takes a **file path**, not a numpy array — but the Protocol hands `np.ndarray`. Two bridges: (a) write the short utterance to a temp WAV (2-10s, negligible cost) — simplest/robust, ship this first; or (b) use the lower-level `transcribe_stream()` context manager which accepts in-memory `mx.array` chunks, feed the whole utterance, read `result.text`. No documented array-input on public `transcribe()` was found — confirm against the installed version.

**Tradeoff.** ~2 GB RAM for the MLX model (vs base.en int8 ~150 MB) — the real cost. The oft-quoted 66 MB ANE figure is **FluidAudio's Swift/CoreML path, NOT reachable from Python** — don't expect it here. English-only on v2. **Dep cost: one pure-MLX package, no torch.**

### #3 — Optional LLM cleanup / self-correction decorator (closes the Wispr gap, but lower value here)

**What.** A decorator STT — `LLMCleanupSTT(inner: STT, ...)` — wrapping any engine (#1 or #2): run `inner.transcribe()`, then a cheap LLM pass to collapse self-corrections ("go to line 40, no wait, 50" → "50"), fix command/proper-noun garbles against known vocab, normalize dictated commands. This is literally Wispr's architecture (ASR → fine-tuned Llama formatter).

**Evidence.** Documented pattern for local Wispr alternatives — transcribe then local LLM (Ollama) fixes punctuation/fillers/self-corrections ([Murmur](https://everydayaiwithbrian.com/blog/replace-wispr-flow.html), [LM-Kit](https://docs.lm-kit.com/lm-kit-net/guides/how-to/transcribe-and-reformat-audio-with-llm.html)). Wispr fine-tunes Llama for exactly the self-correction case ([Wispr post](https://wisprflow.ai/post/technical-challenges)).

**Impl sketch.** New class satisfying `STT` + factory wrap (`cfg.stt_cleanup=True`). Backends: **local Ollama** (keeps local-first, no per-min fee) or **Haiku** (talk2me already spawns Claude, so the CLI is present; near-zero added dep, but network call + tokens).

**Why #3 not #1 (honesty).** Payoff is smaller than for Wispr: the consumer is Claude, which already tolerates filler/missing punctuation and handles many self-corrections itself. Cleanup adds **200-800ms to a loop where the user waits** before the agent even starts, and the hard limit applies: **if ASR dropped a word, no LLM pass recovers it.** Make it opt-in, scope to self-corrections + command normalization only, ship after #1/#2.

---

## Per-question evidence

**1. VoiceInk.** whisper.cpp for Whisper + **Parakeet via FluidAudio** (CoreML/ANE) for fast path. Non-model layers: Personal Dictionary (custom words + smart text replacements), context awareness (screen-content reading + auto app/URL detection), Power Mode (per-app pre-configured settings/prompts), push-to-talk (not streaming), AI-assistant mode. Pullable into Python *as techniques*: hotwords/dictionary biasing (→#1), app/context injection (→#1 live-context seed), per-context prompts, and the ANE-efficiency lesson (→Parakeet #2, though the 66MB ANE win is Swift-only). ([README](https://github.com/Beingpax/VoiceInk/blob/main/README.md), [FluidAudio](https://github.com/FluidInference/FluidAudio))

**2. Wispr Flow.** Server-side hybrid: context-conditioned ASR (speaker/context/history) + fine-tuned **Llama** formatting with token-level control; <200/200/200ms ASR/LLM/network budget. Personal dictionary auto-learns from your edits. Context awareness sends active-app metadata, on-screen proper nouns, and in coding contexts **variable/file names**. Self-correction: an LLM trained to follow user edits so it "never makes the same mistake twice." ([technical post](https://wisprflow.ai/post/technical-challenges), [Baseten](https://www.baseten.co/resources/customers/wispr-flow/), [context docs](https://docs.wisprflow.ai/articles/4678293671-feature-context-awareness))

**3. faster-whisper model path on M-series (CPU-only).** base.en fastest but weakest on terms. large-v3-turbo ≈ large-v2 quality at ~4-6× speed, within 1-2% WER, ~1.5 GB int8; distil-large-v3 ~6× faster, within 1% WER ([GigaGPU](https://gigagpu.com/whisper-large-v3-turbo-vs-large-v3-comparison/), [Marie/Medium](https://medium.com/@bnjmn_marie/whisper-large-v3-turbo-as-good-as-large-v2-but-6x-faster-97f0803fa933)). int8 is right on Mac (already used). **Honest constraint:** on CPU, latency scales with model size × audio length — large-v3-turbo on a 5-10s utterance is several seconds of wait in a live loop. Best whisper-only accuracy-per-second: **small.en** as the conservative bump; turbo only if you accept CPU latency. This is exactly why #2 (GPU Parakeet) beats staying on Whisper.

**4. Parakeet-MLX.** More accurate than Whisper large-v3 + dramatically faster on M-series GPU; English-only v2 (25 EU langs v3); streaming via `transcribe_stream()`; mature Python API (`from_pretrained`/`transcribe`); **no torch/NeMo**. Cost ~2 GB RAM. VoiceInk fast mode = Parakeet confirmed.

**5. Practical faster-whisper tricks.** Covered in #1: hotwords > initial_prompt for proper nouns; `temperature=0.0`, `beam_size=5`; explicit `language="en"`; conversation-context seed. A cheap LLM post-pass (#3) closes the *last* mile but is lower-ROI for an agent consumer. Mic quality matters more than model size for accuracy.

**6. WhisperX — wrong tool here, confirmed.** Value = word-level timestamps + speaker diarization + batched long-form throughput. Adds nothing for short single-speaker utterances, handles overlapping speech poorly; built for podcasts/interviews/meetings, not a dictation loop. Leave it out. ([localaimaster](https://localaimaster.com/blog/whisperx-guide), [repo](https://github.com/m-bain/whisperx))

---

## What Wispr does that you CANNOT match locally

- **Server-side context-conditioned ASR** trained on aggregate usage — a fine-tuned acoustic model adapting to speaker/context isn't reproducible from open weights. Locally you get generic Parakeet/Whisper + prompt/hotword biasing — real but weaker.
- **The <700ms end-to-end ASR+LLM budget** — Wispr hits it with datacenter GPUs + a small fine-tuned Llama. A local LLM cleanup pass on M-series adds hundreds of ms; you can't have both the LLM layer *and* Wispr's latency locally — pick one.
- **Continuous online personalization** ("never makes the same mistake twice" from your edits). Locally you can fake the static half (a growing hotwords/dictionary file), not the online-learning half.
- **OS-wide screen/app context extraction** (IDE variable names, recent chat). talk2me can inject its own conversation context cheaply (#1), not arbitrary on-screen content.

**Net:** shipping #1 + #2 gets talk2me to "clearly better than base.en, GPU-fast, term-accurate" — the ceiling that matters for feeding a coding agent. #3 narrows the self-correction gap. The remaining distance to Wispr is its server-side personalized ASR + sub-700ms LLM formatting — a cloud-economics advantage, not a portable technique.

## Sources

- [VoiceInk README](https://github.com/Beingpax/VoiceInk/blob/main/README.md) · [FluidAudio](https://github.com/FluidInference/FluidAudio) · [FluidAudio ANE writeup](https://macparakeet.com/blog/fluidaudio-speech-ai-sdk/)
- [Wispr technical challenges](https://wisprflow.ai/post/technical-challenges) · [Wispr on Baseten](https://www.baseten.co/resources/customers/wispr-flow/) · [Wispr context awareness](https://docs.wisprflow.ai/articles/4678293671-feature-context-awareness)
- [parakeet-mlx repo](https://github.com/senstella/parakeet-mlx) · [PyPI](https://pypi.org/project/parakeet-mlx/) · [Python API](https://deepwiki.com/senstella/parakeet-mlx/3.2-python-api) · [Parakeet v3 vs Whisper](https://whispernotes.app/blog/parakeet-v3-default-mac-model) · [Parakeet vs Whisper detail](https://www.parakeety.com/resources/parakeet-vs-whisper)
- [faster-whisper repo](https://github.com/SYSTRAN/faster-whisper) · [transcribe options](https://deepwiki.com/SYSTRAN/faster-whisper/4.3-transcription-options-and-configuration) · [turbo vs v3](https://gigagpu.com/whisper-large-v3-turbo-vs-large-v3-comparison/) · [Whisper best settings](https://www.saytowords.com/blogs/Whisper-Best-Settings/)
- [WhisperX guide](https://localaimaster.com/blog/whisperx-guide) · [WhisperX repo](https://github.com/m-bain/whisperx)
- [Local LLM cleanup (Murmur)](https://everydayaiwithbrian.com/blog/replace-wispr-flow.html) · [LM-Kit LLM post-processing](https://docs.lm-kit.com/lm-kit-net/guides/how-to/transcribe-and-reformat-audio-with-llm.html)
