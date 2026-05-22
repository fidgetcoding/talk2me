# talk2me — Integration Plan (synthesis merge)

Audience: the lead, at swarm synthesis. Scope: wire the parallel-swarm outputs
(silero VAD, kitten TTS, session persistence, duplex/crash-recovery, CI, tests)
into a clean tree without conflicts. Read this top-to-bottom before the first merge.

Baseline at time of writing: `main` is clean, working tree empty, single
provider per slot (`energy` VAD, `whisper` STT, `say`/`null` TTS,
`ClaudeCodeBackend`). All seams already exist — every new provider is one factory
branch + one class behind a Protocol. Nothing below requires an orchestrator or
CLI signature change.

---

## 1. factory.py — the exact branches to add

`talk2me/factory.py` uses a strict pattern: each builder is `if cfg.<slot> ==
"<name>": from .<pkg> import <Class>; return <Class>(...)` and falls through to
`raise ValueError(f"unknown <slot>: {cfg.<slot>!r}")`. Match it exactly.

### 1a. Silero VAD — insert in `build_vad(cfg)` (currently lines 20–29)

Add a branch BEFORE the `raise`, after the existing `energy` branch (line 28).
The Silero class must satisfy the `VAD` Protocol (`sample_rate`,
`frame_samples`, `is_speech(frame) -> bool`, `reset()`). Silero expects 16 kHz
and a fixed window (512 samples @ 16 kHz); the swarm's `vad/silero.py` owns that
detail — factory only passes the same three knobs `EnergyVAD` gets:

```python
    if cfg.vad == "silero":
        from .vad import SileroVAD

        return SileroVAD(
            sample_rate=cfg.sample_rate,
            frame_samples=frame_samples(cfg),
            threshold=cfg.silero_threshold,
        )
```

Requires `vad/silero.py` to export `SileroVAD` and `vad/__init__.py` to add it to
`__all__` (see §4 collision note — that `__init__.py` is touched by two agents).

### 1b. Kitten TTS — insert in `build_tts(cfg)` (currently lines 43–52)

Add a branch BEFORE the `raise` (line 52), alongside `say` and `null`. The
KittenTTS class must satisfy the `TTS` Protocol (`sample_rate` attr +
`async synthesize(text) -> AsyncIterator[np.ndarray]` yielding float32 mono PCM).
`SayTTS` takes `voice=cfg.voice`; mirror that so the existing `--voice` flag and
`cfg.voice` field are reused rather than adding a parallel one:

```python
    if cfg.tts == "kitten":
        from .tts import KittenTTS

        return KittenTTS(voice=cfg.voice, model=cfg.kitten_model)
```

If `kitten/__init__.py` exposes `KittenTTS` via `from .tts import KittenTTS`,
`tts/__init__.py` must add the export (see §4 — second double-touched file).
If the swarm placed it at `tts/kitten.py` but did NOT update `tts/__init__.py`,
use the direct form to avoid depending on the package re-export:

```python
    if cfg.tts == "kitten":
        from .tts.kitten import KittenTTS

        return KittenTTS(voice=cfg.voice, model=cfg.kitten_model)
```

Prefer the direct-module import (`from .tts.kitten import KittenTTS`) — it
matches the existing `null` branch (`from .tts.null import NullTTS`, line 49) and
sidesteps the `tts/__init__.py` collision entirely. Recommended.

### 1c. No change to `build_stt` or `build_backend`

The swarm is not adding an STT provider. `build_backend` gains no new branch —
crash recovery lives inside `ClaudeCodeBackend` / orchestrator, not the factory.
Session persistence is a Config field threaded into the existing
`ClaudeCodeBackend(session_id=...)` ctor arg (see §2c), so `build_backend`
(lines 55–64) gets ONE new kwarg, not a branch:

```python
    return ClaudeCodeBackend(
        claude_bin=cfg.claude_bin,
        model=cfg.model,
        cwd=cfg.cwd,
        permission_mode=cfg.permission_mode,
        session_id=cfg.session_id,        # <-- new line, threads persistence
        extra_args=cfg.extra_claude_args,
    )
```

`ClaudeCodeBackend.__init__` already accepts `session_id: str | None = None`
(claude_code.py line 47) and falls back to a fresh uuid4 — so passing
`cfg.session_id` (default `None`) is backward-compatible and needs no backend
edit. This is the cheapest possible wiring for resume.

---

## 2. Config fields the new features need

All additions go in `talk2me/config.py` `@dataclass Config`. Add them inside the
matching section comment so the diff is localized and the lead can drop them in
without re-reading the whole dataclass. Defaults preserve today's behavior
exactly (every default = current runtime).

### 2a. Silero VAD — under the `# --- VAD / turn detection ---` block (after line 27)

`cfg.vad` already documents `"energy" | "silero"` (line 24) — the value is
anticipated; only the threshold field is missing. Silero emits a 0–1 speech
probability, so it needs its OWN threshold (do NOT reuse `energy_threshold`,
which is an RMS value ~0.012 and semantically unrelated):

```python
    silero_threshold: float = 0.5   # silero speech-probability cutoff (0..1)
```

Optional, only if `vad/silero.py` wants to pin the model rev:

```python
    silero_model_repo: str = "snakers4/silero-vad"  # torch.hub repo for the VAD
```

### 2b. Kitten TTS — under the `# --- TTS ---` block (after line 36)

`cfg.tts` already lists `"say" | "kitten" | "null"` (line 35) — value
anticipated. `cfg.voice` (line 36) is shared and reused by the kitten branch
(§1b), so the only new field is the model id:

```python
    kitten_model: str | None = None   # kitten voice/model id; None = engine default
```

### 2c. Session persistence — new section block (after the `# --- misc ---` group)

```python
    # --- session persistence ---
    session_id: str | None = None      # resume an existing claude session; None = fresh uuid4
    session_store: str | None = None   # path to persist/restore session id across runs; None = off
```

`session_id` threads straight into `build_backend` (§1c) with zero backend
changes. `session_store` is the on-disk pointer the swarm's `session.py` reads
and writes — keep it `None`-default so the loop behaves identically when unused.
`session.py` should be the ONLY reader/writer of the store file; factory and
backend stay store-agnostic and see only the resolved `session_id`.

### 2d. Duplex flags — already present, do NOT re-add

`barge_in: bool = True` (line 39) and `half_duplex: bool = False` (line 40)
already exist. The swarm's orchestrator/config duplex work must EDIT semantics
around these existing fields, not introduce new ones. If a swarm diff adds a
third duplex bool, that is a collision — collapse it onto these two (see §4).

### CLI surface (`__main__.py`) — lead's call, not blocking

`config.py` defaults make every new field optional, so the loop runs without any
CLI change. To expose them, add to `_parse_args` (lines 17–55) mirroring the
existing pattern, and extend the `--tts` `choices` list (line 24) from
`["say", "null"]` to `["say", "kitten", "null"]`, plus a `--vad` arg (none exists
today — `cfg.vad` is only settable programmatically right now). This is additive
and conflict-free as long as ONE agent owns `__main__.py` at synthesis.

---

## 3. Collision-free merge / commit sequence

Order chosen so each step lands on a green tree and the high-collision files
(`config.py`, `factory.py`, `__init__.py`s, `orchestrator.py`) are touched by
exactly one commit at a time. Lead applies, runs the three test scripts, commits,
moves on. Tests are plain scripts:

```
./.venv/bin/python -m tests.test_segment
./.venv/bin/python -m tests.test_loop_offline
./.venv/bin/python -m tests.manual_backend_check   # needs `claude` on PATH; skip in CI
```

**Step 1 — pure-additive files (zero shared-file risk).** New files only, no
edits to existing modules:
- `LICENSE`
- `.github/workflows/ci.yml`
- `talk2me/vad/silero.py`
- `talk2me/tts/kitten.py`
- `talk2me/session.py`
- `talk2me/input/wispr_spike.py`
- `tests/{test_sentences,test_factory,test_backend_translate}.py`
- audit docs (this `docs/` tree)

Commit. Nothing imports these yet, so `test_segment` + `test_loop_offline` still
pass unchanged. `test_factory` will FAIL here if it asserts the new branches —
land it but expect red until Step 3; or hold `test_factory` to Step 3's commit.

**Step 2 — Config fields (single owner of config.py).** Apply ALL of §2 (silero
threshold, kitten model, session fields) in one commit. Defaults preserve
behavior, so both existing tests stay green. Do this BEFORE factory wiring so
`factory.py` can reference the fields without a forward-reference gap.

**Step 3 — factory wiring (single owner of factory.py + the __init__ exports).**
Apply §1a, §1b (prefer direct-module import), §1c in one commit. If you used the
package-import form, also touch `vad/__init__.py` and `tts/__init__.py` here.
Now `test_factory` goes green. Run all three tests.

**Step 4 — orchestrator + duplex / crash-recovery (single owner of
orchestrator.py).** Apply the swarm's duplex-flag handling, backend crash
recovery, and the sentence-chunker abbreviation fix in one commit. This is the
riskiest file — see §4. Re-run `test_loop_offline` (it asserts the exact
half-duplex mute cycle `[True, False, True, False]` and the exact sentence split)
and the new `test_sentences`.

**Step 5 — CLI exposure (single owner of __main__.py).** Add `--vad`, extend
`--tts` choices, add `--kitten-model` / `--session-id` / `--session-store` /
`--silero-threshold` flags. Smoke: `./.venv/bin/python -m talk2me --help`.

**Step 6 — packaging.** If silero pulls `torch`/`onnxruntime` or kitten pulls a
new runtime, add them to `pyproject.toml` `dependencies` (currently numpy,
sounddevice, faster-whisper) — ideally as an extras group
(`[project.optional-dependencies]` `silero = [...]`, `kitten = [...]`) so the
base install stays lean and lazy-imported providers don't force heavy deps on
every user. Commit last so CI in Step 1 isn't gated on uninstalled extras.

Leave everything unstaged for the lead per the run constraints — this sequence is
the lead's commit script, not mine.

---

## 4. Risk note — files two parallel agents may have touched

These are the real merge hazards. Ranked by blast radius.

**`talk2me/config.py` — HIGH.** Targeted by the silero agent, the kitten agent,
the session agent, AND the duplex/orchestrator agent. Four independent diffs to
one dataclass = guaranteed textual conflict if applied raw. Mitigation: §2
consolidates ALL field additions into one Step-2 commit by section. Specific
trap: `cfg.vad` (line 24) and `cfg.tts` (line 35) docstrings already name
`silero` and `kitten` — an agent may have ALSO edited those comment lines,
producing a same-line conflict on a comment. Resolve in favor of keeping the
fuller comment; the literal value list there is cosmetic.

**`talk2me/orchestrator.py` — HIGH.** The duplex agent and the
sentence-chunker-fix agent both live here. The chunker fix targets `_SENTENCE`
(line 31, the regex `r"(.+?[.!?]+)(\s+|$)"`) and/or `_drain_sentences` (lines
118–125) for the abbreviation bug (e.g. "e.g." / "Dr." splitting mid-sentence).
The duplex agent edits `_consume_turn` (lines 76–112) around `set_muted` /
`vad.reset()`. These are different functions — should merge cleanly IF both
agents kept their edits localized. Trap: `test_loop_offline` hard-asserts the
exact spoken split `["Two plus two is four.", "Anything else?", ...]` and the
exact mute log `[True, False, True, False]`. A chunker change that alters
sentence boundaries OR a duplex change that alters mute timing will BREAK this
test even if both are individually correct. The lead must reconcile the test's
golden values against the intended new behavior — do not blindly "fix" the test
to pass; confirm the new split/mute sequence is the desired one first.

**`talk2me/vad/__init__.py` and `talk2me/tts/__init__.py` — MEDIUM.** Each is a
3-line export file (`from .x import X` + `__all__`). The provider agent adds its
class; the factory agent may ALSO add it. Two agents appending to the same
`__all__` list = conflict. Mitigation: §1b recommends the direct-module import
(`from .tts.kitten import KittenTTS`) so `tts/__init__.py` need not change at
all; do the same for VAD if `vad/silero.py` can be imported directly
(`from .vad.silero import SileroVAD`) — that drops this risk to zero. Pick the
direct form unless a test imports via the package root.

**Duplex flags — MEDIUM.** `barge_in` and `half_duplex` already exist
(config.py lines 39–40). The duplex agent may have introduced a NEW field
(e.g. `duplex_mode: str`) instead of reusing them, or flipped a default. Trap:
`half_duplex=False` today but the orchestrator docstring says half-duplex "is
what runs today" and `_consume_turn` unconditionally mutes — i.e. the code is
half-duplex regardless of the flag right now. If the duplex agent makes the flag
actually gate behavior, default-flip risk is real: keep the *observable* default
identical to today (mic mutes while agent speaks) or `test_loop_offline`'s mute
assertion breaks. Reconcile any new duplex field down onto the two existing
bools.

**`tests/manual_backend_check.py` — LOW.** Not in the swarm's file list but it
spawns real `claude`; the `test_backend_translate.py` agent may add an offline
translate test that overlaps its intent. No file conflict (different filenames),
but confirm `test_backend_translate` targets `ClaudeCodeBackend._translate` /
`_translate_stream_event` (claude_code.py lines 187–236) as pure-function tests
— that's the correct seam for offline coverage and shouldn't touch the manual
script.

**`pyproject.toml` — LOW but easy to miss.** Heavy new deps (torch for silero,
kitten's runtime) are NOT in `dependencies` today. If two agents both edit the
`dependencies` array they conflict; route all dep changes through the single
Step-6 commit and prefer optional-extras groups.

---

## Quick-reference: symbol/line targets

| Change | File | Anchor |
|---|---|---|
| silero branch | factory.py | after L28, before L29 `raise` |
| kitten branch | factory.py | after L51, before L52 `raise` |
| session_id arg | factory.py | build_backend L58–64 |
| silero_threshold | config.py | after L27 |
| kitten_model | config.py | after L36 |
| session fields | config.py | after L44 |
| duplex semantics | orchestrator.py | _consume_turn L76–112 |
| chunker fix | orchestrator.py | _SENTENCE L31 / _drain_sentences L118–125 |
| VAD Protocol contract | protocols.py | L19–31 |
| TTS Protocol contract | protocols.py | L44–57 |
| backend session_id ctor | backends/claude_code.py | L47, L53 |
