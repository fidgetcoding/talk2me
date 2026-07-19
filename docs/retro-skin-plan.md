# Retro skin — the detailed gameplan

## Versioning: this is v2, and v1 stays reachable (added 2026-07-19)

The retro skin ships as **talk2me v2**. The launch build is preserved as a
first-class citizen, not just a commit hash:

- **Tag `v1.0.0`** — immutable pointer at the launch build (pyproject stamped
  `1.0.0` in its final commit). `git checkout v1.0.0` forever.
- **Branch `v1`** — the same commit as a live line, in case v1 ever needs a
  fix without taking the v2 UI.
- **Installer pinning** — `install.sh` takes `--ref <branch|tag>` (`--v1`
  shorthand), so anyone (Nate included) installs the old build with
  `curl … | bash -s -- --v1`. Re-running with a different `--ref` switches
  versions in place.
- **GitHub Release v1.0.0** — visibility + a frozen zip for non-git users.
- **`main` = the v2 line** — pyproject bumped to `2.0.0` at the start of this
  work; tag `v2.0.0` + release when Phase 5 lands.
- Belt-and-suspenders: once Phase 2 lands, `--plain` inside v2 reproduces
  v1's exact output anyway (that's the parity constraint) — the tag/branch
  protect against the case where that guarantee ever has to bend.

Goal: make talk2me's terminal output beautiful — retro-terminal vibe matching
the banner (green on black, pink + blue accents, dotted borders) — WITHOUT
touching the voice loop's behavior, timing, or tests. Not a full-screen TUI;
still a scrolling conversation, just a gorgeous one.

## Hard constraints (the "won't fuck up what we have" list)

1. **The voice loop is sacred.** No changes to segmentation, VAD, barge,
   pause, permission, pipeline, or transcript logic. Rendering only.
2. **`--plain` forever.** The current output stays available behind a flag and
   is the AUTOMATIC fallback when stdout isn't a TTY, `NO_COLOR` is set, or
   the rich import fails. A broken paint job must never mute the product.
3. **Copy-paste debuggability survives.** Every bug tonight was debugged from
   pasted scrollback. Prose and transcripts stay linear plain lines (colored,
   not boxed); fancy panels are reserved for the transient work region only.
4. **Tests keep passing untouched.** Suites assert state (sent lists, mute
   logs, spoken lists), not stdout — and tests run non-TTY, so they get the
   Plain renderer automatically.
5. **No Textual, no alternate screen, no curses.** Rich only, and only its
   boring parts (Console, Live, Panel, Spinner).

## Architecture: the renderer seam

New module `talk2me/render.py`:

```python
class Renderer(Protocol):
    def startup(self, cfg, save_path) -> None        # banner + config card
    def listening(self) -> None                      # 🎧
    def you(self, text, kind="") -> None             # kind: "", "continued", "barge-in"
    def agent_begin(self) -> None                    # 🤖
    def agent_delta(self, text) -> None              # streaming prose
    def agent_end(self) -> None
    def tool(self, name, detail="") -> None
    def working(self, tool_count, elapsed_s) -> None # ticker's visual half
    def status(self, msg, style="info") -> None      # pause/resume/noise/hints/errors
    def debug(self, msg) -> None                     # [t] lines etc.
    def permission_ask(self, tool, detail) -> None
    def permission_heard(self, heard, decision) -> None
    def permission_verdict(self, tool, allowed) -> None
    def goodbye(self) -> None
    def close(self) -> None                          # MUST restore terminal state
```

- `PlainRenderer`: reproduces today's strings. Byte-parity is the acceptance
  test (one documented exception: the double `[tool]` print, which dies in
  Phase 1).
- `RetroRenderer`: rich-backed.
- Selection in `__main__`: `--plain` flag OR non-TTY OR `NO_COLOR` OR rich
  ImportError → Plain. Else Retro.
- `segment.py`'s `--debug` ▶/⏹ prints stay as-is (diagnostic output, out of
  scope — keeps the segmenter dependency-free).

## The print inventory (every site that moves behind the seam)

orchestrator.py: warmup line · ready banner · config line · working-on line ·
half-duplex hint · 🎧 listening · [t] stt/first-token/first-audio · noise-ignored ·
pause/resume/still-paused · (…waiting for the rest) · 🗣 you (3 kinds) ·
noise-interrupt-resend · 🤖 + streaming deltas · [tool] · ⚙ working ·
[permission] ask/heard/verdict · [go on…]/[barge-in] listening · backend
errors. __main__.py: 📝 transcript line · speaker-downgrade notice · device
error · bye · text-mode prints (text mode stays Plain always — it's a REPL).

## Phase 1 — Data enrichment (no visual change)

- Backend `_translate_assistant_message`: for each `tool_use` block, extract a
  human summary from its input — Write/Edit/Read → `file_path` basename,
  Bash → first ~60 chars of `command`, Grep/Glob → pattern, else "".
  Populate the existing (unused) `ToolActivity.summary` field.
- **Dedupe strategy** (kills the double-print bug): the stream path
  (`content_block_start`) emits the EARLY event (name only, low latency);
  the full-message path emits the DETAIL for the same block. Backend tags
  the second as an upgrade (`ToolActivity(name, summary, upgrade=True)`);
  renderers replace the last matching entry instead of appending; Plain
  prints the detail as an indented follow-on only when present.
- SessionLog gains the detail: `- 🔧 Write (pong.html)`.
- Tests: extend test_backend_translate for summary extraction + upgrade
  tagging; sessionlog assertion.

## Phase 2 — The seam (highest-care phase)

- Write `render.py` with `PlainRenderer`.
- Sweep orchestrator/__main__: every print site → `self.render.*`. Orchestrator
  takes `renderer=` kwarg (default `PlainRenderer()` — tests construct without
  it and get parity for free).
- New `tests/test_render.py`: snapshot every PlainRenderer method against the
  exact current strings (this is the parity proof, written BEFORE the sweep).
- Full 12-suite run must be green with zero edits to existing suites.

## Phase 3 — Retro core (colors, no Live yet)

- Theme (banner-matched): green `#a8dd00`-family for agent prose + frames,
  pink `#ff5fd7` for the user, cyan `#5fd7ff` for system/info states, dim
  grey for [t]/debug. Rich `Theme` object, hex-pinned.
- Startup: config as a small dotted-border card (rich Panel, custom box using
  `┄`/`┆` to echo the banner), banner line with the @fidgetcoding credit.
- Speakers: `▸ you` pink label, agent prose plain green text (linear, no box).
- Status chips: `⏸ paused`, `▶️ awake`, `[barge-in]`, `[go on…]` colored.
- **Security: `markup=False, highlight=False` on every call that carries
  agent/user/tool content** — a reply containing `[red]` must render
  literally, not execute rich markup.
- RetroRenderer smoke tests via `Console(file=StringIO, force_terminal=True)`:
  no raises, injection test, color-code presence.

## Phase 4 — The live work panel (the headline)

- ONE rich `Live` region, active only during the tool phase. Rule: any prose,
  status, or permission call first collapses the Live panel into a permanent
  one-line summary (`⚙ 9 tools · 42s`) then prints. `agent_end`/`close` also
  collapse. This is the interleave discipline that prevents Live × streaming
  corruption.
- Panel content: spinner (rich animates it free), last ~5 tool lines WITH
  detail, tool count, elapsed. Replaces the `⚙ still working…` line spam
  entirely in Retro (Plain keeps the lines).
- The AUDIO tink keeps its own 8s cadence in the orchestrator — unchanged.
- Ctrl-C safety: `close()` in run()'s finally; rich Live.stop() restores the
  terminal; wrapped in try so a dead console can't mask the real exception.
- Ordering test: tool → agent_delta sequence produces collapse-summary before
  prose in the captured output.

## Phase 5 — Polish (optional, separately shippable)

- File-change stats after Edit/Write (needs tool_result surfacing from the
  user-turn blocks — new backend event, evaluate cost then).
- README screenshot of the retro session + a "Ways it looks" blurb.
- `--theme` hook if anyone asks for non-green.

## Risk register

| Risk | Mitigation |
|---|---|
| Rich markup injection from agent/user text | markup=False everywhere content flows; test locks it |
| Live region × streamed prose interleave | collapse-before-print rule (Phase 4), ordering test |
| Ctrl-C during Live corrupts terminal | close() in finally; Live.stop() restores; try-wrapped |
| CI / pipes get ANSI garbage | auto-Plain on non-TTY + NO_COLOR |
| rich version drift | pin `rich>=13,<15` in core deps |
| Paste-debugging degraded | prose stays linear; panels transient-only |
| Parity regression in the sweep | snapshot tests written before the sweep; suites untouched |

## Order of commits (each green + pushed before the next)

1. `feat: tool detail + dedupe (ToolActivity.summary)`  — Phase 1
2. `refactor: renderer seam + Plain parity snapshots`   — Phase 2
3. `feat: retro renderer core (theme, card, chips)`     — Phase 3
4. `feat: live work panel`                              — Phase 4
5. `polish: stats/screenshot/docs`                      — Phase 5

Estimated: P1 ~30m · P2 ~60-75m · P3 ~60m · P4 ~45-60m · P5 open.
Abort semantics: stopping after ANY commit leaves a fully working product.
