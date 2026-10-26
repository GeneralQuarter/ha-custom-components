"""Minimal ``resource`` stand-in so Home Assistant's test harness imports on Windows.

Same reason as the ``fcntl`` shim next to it: ``pytest-homeassistant-custom-component``
loads ``homeassistant.runner`` (via ``patch_time``), which imports
``homeassistant.util.resource``, which imports the Unix-only ``resource``
module. Under test only ``set_open_file_descriptor_limit`` is reachable and it
swallows ``OSError``/``ValueError``, so a no-op implementation is enough.
"""

from __future__ import annotations

RLIMIT_NOFILE = 0
RLIM_INFINITY = -1


def getrlimit(_resource: int) -> tuple[int, int]:
    """Report a soft/hard limit pair that is already large enough."""
    return (8192, 8192)


def setrlimit(_resource: int, _limits: tuple[int, int]) -> None:
    """Accept, but ignore, a limit change."""
