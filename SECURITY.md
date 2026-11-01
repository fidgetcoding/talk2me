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

It's a seatbelt, not a sandbox (`--sandbox`, below, is the sandbox). It matches the
text of a command, and there are endless ways to spell a command. Measured against the real CLI
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

So on its own, the gate is the protection, and it's only as good as the person
answering it. It can't tell your voice from anyone else's, so someone in the room
can say "approve" too. `--voice-lock` narrows that, measured on recordings rather than
a live room (README), and it has a hole: clips under 0.8 seconds always pass, so a
quick "approve" from someone else still lands.

## The real wall: `--sandbox`

`t2m --sandbox` runs the agent inside an OS sandbox: `sandbox-exec` on macOS,
bubblewrap on Linux. The kernel refuses the operation whichever process asks, so
it covers Bash, any script Bash runs, and the agent's own Write and Edit tools.

Inside, the agent can:

- write to the project, a private temp folder, and its own session state
- reach the network only through a small proxy talk2me runs outside the sandbox,
  which tunnels to the model API on port 443 and refuses everything else, DNS
  included
- read most of the disk, except `~/.ssh`, `~/.aws`, `~/.gnupg`, browser data, mail
  and the other usual secret stores (the login keychain stays reachable, see below)

It's off by default because it breaks normal things: `npm install` needs
`--sandbox-allow-host .npmjs.org`, and on a Mac, reaching a local dev server or
database needs `--sandbox-localhost`. If the sandbox can't run, talk2me refuses to
start instead of quietly running without it. Windows has no sandbox wired up, so
`--sandbox` refuses there.

I tested it with a hostile script, not just a policy file. `tests/test_sandbox.py`
runs an actual script under the actual sandbox in a fake home, on Mac and Linux in
CI. It tries to edit shell profiles, plant git and agent hooks, write through
symlinks and hard links, read SSH keys, connect out, resolve DNS and tunnel through
the proxy to a stranger, and it checks the disk afterwards from outside. The same
script runs with no sandbox first, so a pass can't mean the attack was broken.
`tests/manual_sandbox_check.py` does it through the real `claude` CLI: its own Write
tool, then a script it wrote and ran.

What it still can't do:

- **The project is writable, by design.** A bad instruction can still wreck
  uncommitted work. Hooks, git config and agent settings inside the project stay
  read-only, because those would run later outside the sandbox. On Mac that also
  means `git init` fails inside it. On Linux only the ones that already exist get
  locked.
- **The model API is reachable, also by design.** A script could send data to
  `api.anthropic.com` using somebody else's key. Every host you add with
  `--sandbox-allow-host` is another way out.
- **Your login is reachable.** The agent has to authenticate, so on Linux
  `~/.claude/.credentials.json` stays readable, and on a Mac a script can use the
  keychain the same way the CLI does. It also means the keychain files stay writable
  on a Mac, because the CLI rotates its login token there, so a script could damage
  them. It can't send what it finds anywhere but the allowed hosts.
- **The Mac's sandbox is deprecated and undocumented.** Apple still ships
  `sandbox-exec` and marks it deprecated. The rules that stop launching things
  outside it (`open`, `osascript`, `launchctl`) go by path plus a list of system
  services, so a purpose-built native binary asking launchd directly isn't something
  I've ruled out.
- **Kernel bugs are out of scope.** For anything you can't afford to lose, a
  container or VM with no credentials you'd miss is still the stronger answer.

`--agent codex` doesn't use any of this: Codex runs in its own sandbox
(`workspace-write`, no network) and never asks.

Every utterance-to-execution path is covered by headless tests, and the CI
runs them all on every push.

Found something? Open a GitHub issue — or email nate@lorecraft.io for anything
sensitive.
