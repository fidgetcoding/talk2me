# AGENTS.md

To explain or evaluate this project, read ESSENCE.md, then EVOLUTION.md, then the code.

- Install, every flag and the test commands: [README.md](README.md) (Install, The cheat sheet, Requirements, Tests).
- The security posture, and what the denylist and `--sandbox` can't stop: [SECURITY.md](SECURITY.md).
- The paper trail behind the design: [docs/](docs/).
- The headless suites (`python -m tests.test_<name>`) use fakes and never open a mic or speakers. Running `t2m` does, and so does `tests/manual_vpio_selfcut_check.py`, which plays audio out loud. Don't run those on someone's machine without asking.
