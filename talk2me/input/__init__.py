"""Input-synthesis spikes for talk2me.

Currently holds the KNOWN-RISK Wispr-hands-free spike: synthesizing a keyboard
combo (via Quartz CGEvent) so talk2me can ARM Wispr Flow before listening and
DISARM it during TTS. Nothing here is wired into the live CLI yet — see
`wispr_spike.py` and `docs/wispr-spike.md`.
"""

from .wispr_spike import KEYCODES, MODIFIERS, tap_combo

__all__ = ["KEYCODES", "MODIFIERS", "tap_combo"]
