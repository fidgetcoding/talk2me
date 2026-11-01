# How talk2me got built

My words are quoted as I wrote or said them, typos fixed, nothing else. Commit hashes are from this repo, and the releases are tagged on GitHub. "Private note" means a dated record I keep offline.

## 1. The short version

On May 22, 2026, I had Claude Code build a half-duplex voice loop over its own streaming mode. It passed headless tests, and I never once spoke to it. It sat for eight weeks.

On the night of July 18 I said "let's finish talk2me". By 2 AM it had spoken approvals, barge-in, a GPU speech engine and its first real conversations, including a pong game built by voice. The next two days were a fight with physics: how do you talk over a laptop that's talking? Voice-lock shipped and seemed to lose. An echo gate shipped and half won. Public `main` got rolled back to v2.2.0. macOS's own echo canceller finally won in v2.5.

On November 1 a safety audit flagged what the project's first audit had already warned about: voice shouldn't auto-approve tools. v2.6 fixed that, grew the denylist from 8 rules to 69 after testing it against the real CLI, and pinned the installer to tags. The same day, GPU Whisper became the default ears, then I reversed it.

Then I went after everything on my own "isn't proven" list, and found the biggest thing I'd gotten wrong. For three versions voice-lock mostly just observed, and I blamed laptop mics. It was a feature bug: I was feeding the speaker model the wrong input. With that fixed, on recordings of 40 real speakers through a simulated laptop room, strangers let in fell from 93.5% to 6.3%.

## 2. Origin, in my words

From the README:

> "I live in Claude Code. All day, in the terminal, sometimes from my phone on the couch over SSH."

> "I'd dictate a prompt, then sit there reading a wall of text like it's 1997."

> "Nobody had built that for a terminal coding agent without scraping the screen like a raccoon going through a dumpster. So here we are."

The scoping note (private note, May 22) has the turn, step by step:

> "Every turn = hit Fn, talk, release, wait, read output, hit Fn again. No conversation, just a sequence of dictations."

## 3. Timeline

| Date (2026) | What happened | Evidence |
|---|---|---|
| May 22 | Half-duplex loop over stream-json. VAD, TTS, session persistence, audit reports, MIT | `dd2c8df`, `29336e0`, `7b4fcdb`, `6bf634f` |
| Jun 12 | Bug-hunt sweep | `2943d27` |
| Jul 18 | Spoken tool approval, barge-in, Parakeet, latency batch, continuation stitching | `06200a3`, `2c53cbc`, `d3bc338`, `81c41e7`, `2ec892c` |
| Jul 18 | First live-mic session passes on defaults | Private note |
| Jul 19 | Auto-approve becomes the default after approvals proved unusable live | `8712038` |
| Jul 19 | v1.0.0 launch build, then v2 retro face, wizard, phone mode, Codex, session resume | `4e39d7d`, `b6baf3e`, `f7f5024`, `82841ee`, `7fa0239`, `04bfbb9` |
| Jul 19 | Voice-lock v2.3, then "can never reject its owner", then degraded mode observes | `6eeb35f`, `aeb7fee`, `c6854c5` |
| Jul 19 | Echo gate v2.4, plus a run of deadlock and self-barge fixes | `04550c3`, `48214fa`, `50f6d9f`, `624e7e1` |
| Jul 20 | Low-band human channel built, then killed by field data | `5434b9a`, `c0d2db4` |
| Jul 20 | Public `main` rolled back to v2.2.0 with the honest story | `a7bd5d4` |
| Jul 20 | Native AEC: gameplan, spike goes, capture behind the mic seam, field round | `e6e3f45`, `1b17d06`, `b5a0beb`, `68aa2d6` |
| Jul 20 | v2.5.0 ships, rollback over. Typed takeover | `3085fd9`, `cc2877d` |
| Aug 17 | Private paths and names scrubbed | `686b4df`, `5ecf51e` |
| Nov 1 | v2.6.0: GPU Whisper as default, gated voice, 69-rule denylist, tag-pinned install, ears race | `cc130d2`, `79d5866`, `6690c50`, `0098776` |
| Nov 1 | I reverse the default: base.en stays, GPU Whisper opt-in | `588ad25` |
| Nov 1 | Race made fair: both Whispers run with the same terms | `60e3afa` |
| Nov 1 | CI made real on Ubuntu 26.04 with a null audio sink, and ruff pinned | `f66e7e4` |
| Nov 1 | `--sandbox`: the OS enforces the limits, opt-in, macOS and Linux | `73eff4e`, `1133134` |
| Nov 1 | Commands in Spanish, French, German, Italian and Portuguese | `e7bcf6a` |
| Nov 1 | Dead air out of the default voice | `be9a62f` |
| Nov 1 | CI on Windows, then the live loop on Linux virtual sound cards, then the phone page in real browsers | `6cfa415`, `244cce5`, `10edcf2` |
| Nov 1 | The ears measured on recordings of real people | `20783fb` |
| Nov 1 | Voice-lock's real bug found and fixed | `e6ce7b0` |
| Nov 1 | What a new user hits first, from a clean clone, becomes one sentence each | `0bdf843` |
| Nov 1 | v2.7.0 prepared on a release branch | `320466f` |

## 4. Round by round

### Round 1: the spine (May 22)
- **Built:** the loop, driving a headless agent over stream-json instead of hooking the terminal, because Ghostty had no scripting API anyway.
- **Tested:** headless suites only, plus audits. No one spoke to it.
- **Broke:** nothing it could know about. That was the problem.
- **Wanted next:** it went dormant.

### Round 2: first words (July 18)
- **Built:** spoken approve and deny, pinned byte by byte against the real CLI (`docs/permission-spike-results.md`). A control request that really cancels generation, which made barge-in possible.
- **Tested:** the first live session passed on default settings. A spoken approval wrote a file to the Desktop.
- **Broke:** barge-in crashed on a speaker race (PortAudio -9986), and first words got clipped.
- **Fixed:** abort plus deferred close, and 300 ms pre-roll (`834a637`).
- **Wanted next:** better ears, and less waiting. My note: "drastically reduce that downtime."

### Round 3: latency and manners (July 18 to 19)
- **Built:** term-accuracy seeding, Parakeet on the GPU, config isolation, first-clause speech.
- **Tested:** `--debug` receipts per turn. Time to first token on a trivial prompt went from 3.14 s to 1.39 s. Whisper took 0.32 to 0.41 s, and Parakeet 0.11 to 0.26 s.
- **Broke:** phantom turns, "Okay ×26". I rejected the audio-bleed theory, and I was right to. Fan hum tripped the VAD, and Whisper hallucinated on garbage, biased by the agent's own vocabulary.
- **Fixed:** hallucination gate, stutter collapse, then a real speech classifier.
- **Wanted next:** a loop that felt finished, with a face. So v2.

### Round 4: voice-lock (July 19)
- **Built:** enroll once, and the mic only answers to you.
- **Tested:** in the field, on a laptop mic.
- **Broke:** it couldn't reliably tell people apart. I decided your own voice just scatters wider on a laptop mic than the gap to a stranger's. That turned out to be wrong, and Round 8 has the real ending.
- **Fixed:** it can never reject its owner, and it falls back to observing. Scores still log.
- **Wanted next:** talking over speakers, which voice-lock was supposed to unlock.

### Round 5: the echo war (July 19 to 20)
- **Built:** an echo gate that matches the mic against the exact playback, then a low-band human channel.
- **Tested:** zero false cuts in the field for the gate, but a couch-voice talk-over lost near max volume.
- **Broke:** cuts froze the loop, self-barge on prose. And field data killed the low-band premise (`c0d2db4`).
- **Fixed:** deadlocks, one at a time. Then the public branch rolled back to v2.2.0, the last version I'd stand behind (`a7bd5d4`).
- **Wanted next:** the canceller macOS already has.

### Round 6: the one we were chasing (July 20)
- **Built:** spike on Apple's voice processing (`1b17d06`), native capture behind the mic seam, a mode ladder with the gate as fallback.
- **Tested:** the spike showed echo unreadable to the transcriber. A field protocol found the volume sweet spot, four clicks below max.
- **Broke:** webrtc VAD read the voice processor's noise floor as speech.
- **Fixed:** native mode uses the energy VAD.
- **Wanted next:** nothing urgent. It sat until November.

### Round 7: better manners (November 1)
- **Built:** the spoken gate as default, 69 denylist rules, tag-pinned install, a rerunnable ears race, GPU Whisper.
- **Tested:** the old 8-rule denylist against the real CLI. `rm -fr`, `/bin/rm -rf`, `find -delete`, `python3 -c`, `scp`, `nc`, `/usr/bin/curl` and `git -C . push` all ran.
- **Broke:** turbo's no-speech head read 0.0 on silence through MLX. 3 of 24 clips lost closing punctuation, which the hold logic reads as trailing off.
- **Fixed:** a Silero VAD pass first and a base prompt.
- **Reversed:** GPU Whisper as the default. Per the commit, "the default speech model doesn't move, and first run gets no new download" (`588ad25`). It's opt-in now.
- **Wanted next:** to clear my own "isn't proven" list.

### Round 8: the list (November 1)
- **Built:**
  - `--sandbox`, because a denylist reads command lines and a written script walks past it. The kernel enforces it: `sandbox-exec` on macOS, `bwrap` on Linux. It's off by default, since `npm install` and friends then need `--sandbox-allow-host`.
  - Commands in five more languages.
  - Chunks that render side by side, so the default voice stops waiting on itself.
  - A clean first run: one sentence each for no `claude`, no PortAudio, a busy port, a typo'd model.
- **Tested:** see section 6. On recordings of real people, Parakeet came out best on audiobook sentences and worst on one word (75.0% at the gate, 23 false approvals of 600). CI now drives what I'd only ever claimed: the real process through real PortAudio on virtual sound cards, every headless suite on Windows, and the real phone page in Chromium through a real `ssh -L` tunnel.
- **Broke:**
  - Ctrl-C mid-sentence closed the stream under a running write, and ALSA corrupted the heap on Linux.
  - Windows crashed on its first keystroke setup, and a bare `claude` couldn't launch npm's `claude.cmd`.
  - A torn packet hung up on the phone.
  - My first sandbox hid the keychain files, which would have logged a subscription out (`1133134`).
- **Fixed:** all of those.

**The voice-lock ending.** I measured it for the first time on recordings of real people: 40 speakers from LibriSpeech, each enrolled through the real enrollment path. The numbers were terrible, and that's what cracked it. The speaker model expects mean-normalized features, and the first voice-lock commit (`6eeb35f`, July 19) left that out, after a spike the same day wrongly concluded normalizing "inverted its metric". So the bug was never laptop mics. It was mine, and `e6ce7b0` fixes it.

| 40 real speakers, recordings | Before | After |
|---|---|---|
| Simulated laptop room: speakers it enforces for | 4 | 39 |
| Simulated laptop room: strangers let in | 93.5% | 6.3% |
| Simulated laptop room: equal-error rate | 34.0% | 5.2% |
| Simulated laptop room: owner rejected | 2.7% | 9.2% |
| Clean recordings: speakers it enforces for | 5 | 40 |
| Clean recordings: strangers let in | 91.5% | 2.2% |
| Clean recordings: equal-error rate | 21.0% | 1.1% |
| Clean recordings: owner rejected | 1.0% | 0.5% |

The bench is `tests/manual_voicelock_bench.py`: audiobook recordings from files, through the real enrollment, with the other 39 speakers as the strangers. The laptop room is a simulation: synthetic room echo, a laptop-like frequency response, fan-like noise and a random level. "Before" looks generous because a speaker who falls back to observing counts as accepting everyone.

Most owner rejections in the laptop room are on the shortest clips, and anything under 0.8 seconds always passes. Old voiceprints can't be scored the new way, so they keep their recipe and each launch says to re-enroll. It's a filter, and it hasn't heard a real voice through a real mic since the fix.

## 5. Feature family tree

```
"hit Fn, talk, release, wait, read, hit Fn again"
└─ headless agent over stream-json (the spine)
   ├─ spoken approvals → permission wire pinned → denylist → gated default (v2.6) → OS sandbox (v2.7)
   ├─ interrupt control request → barge-in → speakers problem
   │  ├─ voice-lock (observe only, then the feature bug found, enforcing in v2.7)
   │  ├─ echo gate (fallback now)
   │  ├─ low-band channel (killed)
   │  └─ native AEC (v2.5) → typed takeover
   ├─ whisper → hotwords → seeding → hallucination gate → speech classifier
   │  └─ swappable ears → Parakeet → GPU Whisper (opt-in) → ears race → real speakers → voice-lock bug
   ├─ say → first-clause speech → render pipeline → side by side chunks
   └─ renderer seam → retro face, work panel
   hear the normal UI → Stop hook speaker (side product)
   couch over SSH → phone mode → real browsers over a real tunnel in CI
   English commands → five more languages
```

## 6. Test log

| When | What | Result | Where |
|---|---|---|---|
| Jul 18 | First live mic | Pass on default VAD | Private note |
| Jul 19 | Live sessions | Counted to fifty, barged mid-count, pong and a platformer by voice, a bug reported and fixed by voice | Private note |
| Jul 19 | Latency | Time to first token 3.14 s to 1.39 s. Parakeet 0.08 s warm | Private note, `81c41e7` |
| Jul 20 | Native AEC | Spike go, protocol 1 and 2 pre-passed 4 of 4 | `1b17d06`, `c706550` |
| Nov 1 | Denylist on the real CLI | 8 rules: 8 bypasses shown. 69 rules: known gaps listed | `tests/manual_denylist_check.py`, `SECURITY.md` |
| Nov 1 | Ears race, 24 clips, M2 Max | base.en 0.46 s, 12.5% errors. Turbo CPU 6.17 s, 11.2%. Turbo GPU ~0.55 s, 13.2%. Parakeet v2 0.11 s, 13.2%. Parakeet v3 0.12 s, 14.5% | README, `tests/manual_stt_bench.py` |
| Nov 1 | Same race with matching terms | base.en 7.3%, GPU Whisper 6.6%. Best case, since the terms were the answers | `60e3afa` |
| Nov 1 | Under load | About 1 s GPU, 0.66 s base.en, 0.31 s Parakeet. Errors unchanged | `0098776` |
| Nov 1 | Ears on real people, LibriSpeech dev-clean, 40 speakers, 4,367 words | Word errors: base.en 5.6%, turbo 3.5%, Parakeet 1.7% | `tests/manual_real_speech_bench.py`, `20783fb` |
| Nov 1 | Spoken gate on Speech Commands, 400 clips plus 600 unrelated words | Right: base.en 92.5%, turbo 93.0%, Parakeet 75.0%. Opposite verdicts: 0, 0, 1. False approvals: 6, 1, 23 | Same |
| Nov 1 | Voice-lock on 40 real speakers | Table in Round 8 | `tests/manual_voicelock_bench.py`, `e6ce7b0` |
| Nov 1 | Foreign command words on synthetic voices | 35 of 80 before, 68 of 80 on base, 72 of 80 on turbo | `e7bcf6a` |
| Nov 1 | Default voice on a 10-sentence reply, files with a fake speaker | Worst stop 380 to 880 ms before, 16 ms or less after. Restarts 9, then 4 or 5 | `be9a62f`, `tests/manual_voice_gap_check.py` |
| Nov 1 | Live loop on Linux virtual sound cards | 16 of 16 in CI | `244cce5`, `tests/live_linux_audio_check.py` |
| Nov 1 | Phone page in Chromium over a real `ssh -L` tunnel, fake mic from a file | 18 of 18 in CI | `10edcf2`, `tests/live_phone_browser_check.py` |
| Nov 1 | `--sandbox`: a hostile script under the real sandbox, with an unsandboxed control, and through the real `claude` CLI against a fake endpoint | Pass in CI on macOS and Linux | `73eff4e`, `tests/test_sandbox.py`, `tests/manual_sandbox_check.py` |
| Nov 1, `e8bbd5a` | All 9 CI jobs | Green | GitHub Actions |
| Nov 1, `e8bbd5a` | 27 headless suites, Python 3.12, one Mac | 27 of 27 pass, including voice commands 95, flow 73, sandbox 56, echogate 34, permission 34, preflight 31 | Run while writing this, counts from a clean-clone run |
| Nov 1 | Denylist against the real CLI, and the sandbox through the real CLI | 72 of 72, and 9 of 9 | `tests/manual_denylist_check.py`, `tests/manual_sandbox_check.py` |

The voices in the first race are synthetic, so that accuracy is a hint at best. The later benches use real people's recordings, from files.

## 7. Roads not taken

- **Scraping the terminal.** The reason the project exists.
- **Pipecat and LiveKit.** Too much for a local single-user loop.
- **Auto-approve forever.** Tried for four versions.
- **Low-band talk-over.** Built, measured, dead.
- **GPU Whisper by default.** Built, measured, reversed by me.
- **The sandbox on by default.** It would break `npm install` and friends until you allow their hosts, so it's opt-in.
- **Rewriting tags to scrub an old username.** It would break `--v1` and release links. Accepted.

## 8. Before and after visuals

The v2 session:

![A talk2me v2 session](docs/retro-session.svg)

## 9. My method

1. **Pin the wire before you build on it.** Byte-level spike first, in writing.
2. **Tell the screen the truth.** A marker for every state, so silence is never a mystery.
3. **Field-test, then let data kill your idea.** Low-band lost to measurement.
4. **Then doubt your explanation too.** Voice-lock lost to a story I told myself about laptop mics, and better data on real people found the bug.
5. **Roll back in public.** If the new thing isn't good enough, say so and restore the old one.
6. **Keep the paper trail, including the parts that argue with you.** The audit stayed in `docs/`, and it was right.

## 10. What's next

- Talk to this build on a real mic in a real room, and on a real iPhone.
- A streaming voice. Kokoro is the candidate.
- Driving Wispr hands-free, if the keypress trick proves out.
- Real-voice testing for GPU Whisper.
- The rest is for the community. Fork it.

## 11. Sources

- Commits and tags: `git log` of `fidgetcoding/talk2me`, tags v1.0.0 to v2.6.0, release branch `ci/release-v2.7.0`.
- In the repo: `README.md`, `SECURITY.md`, `docs/security-audit.md`, `docs/permission-spike-results.md`, `docs/v2.5-aec-plan.md`, `docs/stt-upgrade-research.md`, `docs/release-notes-v2.7.0.md`, `tests/manual_stt_bench.py`, `tests/manual_denylist_check.py`, `tests/manual_real_speech_bench.py`, `tests/manual_voicelock_bench.py`.
- Private, kept by me: scoping note, May 22. Session capture, July 19. Safety report, November 1 (session notes). Test logs from November 1.
