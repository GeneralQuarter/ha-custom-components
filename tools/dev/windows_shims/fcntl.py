"""Minimal ``fcntl`` stand-in so Home Assistant's test harness imports on Windows.

Home Assistant only supports Linux. ``pytest-homeassistant-custom-component``
imports ``homeassistant.runner`` at plugin load time, which imports ``fcntl``
and therefore aborts on Windows before a single test is collected.

The functions below are only reachable from code paths Home Assistant never
executes under test (process-level locking of a PID file). The module exists so
imports succeed; it does not emulate file locking.

Load it by putting this directory on ``PYTHONPATH`` (see ``tools/dev/run-tests.ps1``).
"""

from __future__ import annotations

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

F_GETFD = 1
F_SETFD = 2
F_GETFL = 3
F_SETFL = 4
FD_CLOEXEC = 1


def flock(*_args: object, **_kwargs: object) -> None:
    """Pretend to take a file lock."""


def lockf(*_args: object, **_kwargs: object) -> None:
    """Pretend to take a POSIX record lock."""


def ioctl(*_args: object, **_kwargs: object) -> int:
    """Pretend to issue an ioctl request."""
    return 0


def fcntl(*_args: object, **_kwargs: object) -> int:
    """Pretend to issue an fcntl request."""
    return 0
