"""Test package. One global guard: suites must never read the developer's
real ~/.talk2me/config.json — saved-setup defaults would silently change
what the parsing/factory suites assert. Point the config path at nothing;
test_wizard overrides it per-case with tempdirs."""

import os

os.environ.setdefault("TALK2ME_CONFIG", "/nonexistent-t2m-test-config")
