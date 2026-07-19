# Security

talk2me runs a coding agent that can be driven by ambient audio, so its security
posture is documented in unusual depth for a hobby-sized repo:

- `docs/security-audit.md` — the standing audit (threat model: RCE-by-sound)
- `docs/permission-handling-design.md` + `docs/permission-spike-results.md` —
  the verified tool-permission wire

## The posture, honestly

The **default is auto-approve** (`bypassPermissions`): tools run without spoken
confirmation, because per-step approvals make a voice loop unusable. What keeps
that sane:

1. **The hard denylist survives every mode, bypass included** — Claude's scoped
   deny rules apply universally, so `sudo`, `rm -rf`, `git push`,
   `git reset --hard`, `curl`, `wget`, `ssh`, and `dd` are blocked outright and
   cannot be approved by voice at all.
2. **`--gated` mode** restores per-call spoken approvals (read-only allowlist
   auto-runs, everything else asks, ambiguity auto-denies), for work where the
   blast radius matters.
3. **The only zero-guardrail posture** (`--dangerously-allow-tools`, which
   drops the denylist too) **refuses to run in voice mode** — ambient audio
   never gets full system access; that flag requires `--text` and a keyboard.
4. Permission-affecting flags (`--dangerously-skip-permissions`, `--add-dir`,
   `--permission-prompt-tool`, `--allowedTools`, `--disallowedTools`) are
   rejected from the CLI passthrough so the posture can't be smuggled around.

Every utterance-to-execution path is covered by headless tests, and the CI
runs them all on every push.

Found something? Open a GitHub issue — or email nate@lorecraft.io for anything
sensitive.
