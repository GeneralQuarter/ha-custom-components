"""Minimal ``aiousbwatcher`` stand-in so Home Assistant's bluetooth stack imports on Windows.

``homeassistant.components.bluetooth`` imports ``homeassistant.components.usb``
for USB Bluetooth adapter discovery, and that module imports
``aiousbwatcher``, which is a CPython extension built on inotify and therefore
cannot be installed on Windows.

The integration only ever talks to a Bluetooth *proxy* over the network, so no
USB watcher is involved. Constructing the watcher raises the same
``InotifyNotAvailableError`` that Home Assistant handles by falling back to
periodic polling, which is the documented path for hosts without inotify.
"""

from __future__ import annotations


class InotifyNotAvailableError(RuntimeError):
    """Raised when inotify is unavailable, mirroring the upstream exception."""


class AIOUSBWatcher:
    """Replacement that reports the missing inotify backend immediately."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """Fail exactly like upstream does on a host without inotify."""
        raise InotifyNotAvailableError("inotify is not available on this platform")
