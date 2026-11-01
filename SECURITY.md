# Security

talk2me runs a coding agent that can be driven by ambient audio, so its security
posture is documented in unusual depth for a hobby-sized repo:

- `docs/security-audit.md` — the standing audit (threat model: RCE-by-sound)
- `docs/permission-handling-design.md` + `docs/permission-spike-results.md` —
  the verified tool-permission wire

## The posture, honestly

Voice defaults to the **spoken gate**, as of v2.6.0. A mic isn't a keyboard: it
hears you, the TV, and whoever just walked in, and Whisper will happily turn any
of that into an instruction. So anything outside a short allowlist pauses the
turn and asks out loud, and if the answer is unclear twice, it declines.

v2.2 through v2.5 went the other way and defaulted voice to auto-approve. If you
ran the setup wizard back then and it saved `"gated": false`, you keep that until
you change it, and each launch says so in one line.

What's in place:

1. **`--gated` is the voice default.** `--auto-approve`, or picking it in
   `t2m --setup`, makes tools run with nobody asking, so anything the mic hears
   can start them. `--text` input still defaults to auto-approve, since nothing
   ambient can reach a keyboard.
2. **A denylist sits on top of every mode, bypass included.** 69 rules covering
   recursive `rm`, `find -delete` and `-exec`, `git push`, `git reset --hard`,
   `curl`, `wget`, `ssh`, `scp`, `rsync`, `nc`, `sudo`, `dd`, interpreter one-liners
   (`python -c`, `node -e`, `bash -c` and friends), absolute-path spellings of
   these, and more. You can't approve them by voice. Deny beats allow, so the
   gate's allowlist can't reopen them.
3. **The only zero-guardrail posture** (`--dangerously-allow-tools`, which drops
   the denylist too) **refuses to run in voice mode.** That flag requires `--text`
   and a keyboard.
4. Permission-affecting flags (`--dangerously-skip-permissions`, `--add-dir`,
   `--permission-prompt-tool`, `--allowedTools`, `--disallowedTools`) are
   rejected from the CLI passthrough so the posture can't be smuggled around.

## What the denylist can't do

It's a seatbelt, not a sandbox. It matches the text of a command, and there are
endless ways to spell a command. Measured against the real CLI
(`tests/manual_denylist_check.py`, results in `docs/permission-spike-results.md`),
Claude Code's matcher is good at the wrappers: `a && b`, pipes, `sh -c '...'`,
`env` and `timeout` all get seen through. These still walk past it, and under
auto-approve they run with nobody asking:

- **A script.** Write a file, then run it. The denylist reads the command line,
  never the file: `./cleanup.sh`, `python x.py`, `make`, `npm run` can contain anything.
- **Renames and aliases.** A copied binary, or `git config alias.p push && git p`.
- **Flags nobody listed**, like `rm -vrf`.
- **Other tools.** The rules cover Bash. `WebFetch` can reach the network, `Write`
  can edit your shell profile, and MCP servers do whatever they do.

The gate's allowlist runs some commands without asking, and a few of those can run
code: `npm run` and `pytest` execute whatever the project defines, and `cat` can
read any file you can. Trim it if that bothers you: `--deny-tool 'Bash(npm run:*)'`.

So the gate is the protection, and it's only as good as the person answering it.
It can't tell your voice from anyone else's, so someone in the room can say
"approve" too. `--voice-lock` narrows that where its calibration holds, but on a
laptop mic it often only observes. For anything you can't afford to lose, run in a
container or a throwaway checkout, with no credentials you'd miss.

`--agent codex` doesn't use any of this: Codex runs in its own sandbox
(`workspace-write`, no network) and never asks.

Every utterance-to-execution path is covered by headless tests, and the CI
runs them all on every push.

Found something? Open a GitHub issue — or email nate@lorecraft.io for anything
sensitive.
