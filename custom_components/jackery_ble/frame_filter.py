"""Duplicate suppression and liveness tracking for inbound BLE frames.

The Smart Meter 1P (and the vault) send every notification twice, roughly
400 ms apart, byte-for-byte identical. Feeding both copies into the
coordinator doubles state writes for no new information, so identical frames
inside a short window are dropped. Liveness is tracked separately: every
notification proves the device is alive, even when its payload is a duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

# Observed duplicate spacing is ~405 ms; keep the window wide enough for jitter
# but far below the 2 s push cadence so a genuinely repeated reading still
# reaches Home Assistant.
DUPLICATE_WINDOW = 1.0

# The CT pushes every ~2 s. Allow a generous grace period before calling the
# data stale, so a missed frame or two does not flap the entities.
STALE_AFTER = 20.0


@dataclass
class FrameFilter:
    """Drop short-lived duplicate frames while tracking the last received one."""

    duplicate_window: float = DUPLICATE_WINDOW
    last_seen: float | None = field(default=None, repr=False)
    _last_frame: tuple[bytes, float] | None = field(default=None, repr=False)

    def accept(self, frame: bytes, now: float | None = None) -> bool:
        """Return True if the frame carries new information.

        ``last_seen`` is refreshed for every frame, including duplicates.
        """
        current = monotonic() if now is None else now
        self.last_seen = current

        if self._last_frame is not None:
            previous, seen_at = self._last_frame
            if previous == frame and current - seen_at < self.duplicate_window:
                # Keep the original timestamp: a static reading that repeats at
                # the push cadence must not be suppressed forever.
                return False

        self._last_frame = (frame, current)
        return True

    def is_stale(self, max_age: float = STALE_AFTER, now: float | None = None) -> bool:
        """Return True when nothing has arrived for longer than ``max_age``."""
        if self.last_seen is None:
            return False

        current = monotonic() if now is None else now
        return current - self.last_seen > max_age
