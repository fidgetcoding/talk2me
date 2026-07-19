# Security

talk2me runs a coding agent that can be driven by ambient audio, so its security
posture is documented in unusual depth for a hobby-sized repo:

- `docs/security-audit.md` — the standing audit (threat model: RCE-by-sound)
- `docs/permission-spike-results.md` — the verified tool-permission wire

Key guarantees: `bypassPermissions` is unreachable from voice input, destructive
tools are hard-denied (never voice-approvable), tool-rule flags cannot be
smuggled through the CLI passthrough, and every utterance-to-approval path is
covered by headless tests.

Found something? Open a GitHub issue — or email nate@lorecraft.io for anything
sensitive.
