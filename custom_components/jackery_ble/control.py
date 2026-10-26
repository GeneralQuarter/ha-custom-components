"""Follow-meter supervisory policy.

A custom-mode plan's ``pw`` is the vault's AC output *target*, not a ceiling.
Measured on 2026-10-23 at 18:23: with ``pw = 2500`` and a ~570 W house (no
PV), the vault pushed ``batOutPw 2590`` and the meter read **-2020 W** - it
happily exports the surplus. Earlier evidence that it "only covers the load"
was taken while the state of charge sat on the discharge floor, so the floor,
not the cap, was doing the limiting.

The cap therefore has to be the house load minus whatever the PV systems on
the same feed are producing. The vault cannot supply that figure: its
``inGridSidePw`` / ``outGridSidePw`` are **not** measurements unless a Smart
Meter is bound to it (``ctStat`` is 0 here), they are derived from the plan.
The only real measurement is the CT clamp on the grid feed, whose signed power
(positive = import, negative = export) is fed in as ``grid_pw``.

Control law: one watt of import is one watt of output still missing, so each
tick adjusts the target by the measured error. Driving the meter to zero also
absorbs the second PV system and stops the battery when the microinverter array exports.

Verified device behaviour this module relies on (documented in the local
development notes):

* the battery discharges only while ``soc > socDischgLimit`` - the floor is
  exclusive, so exact equality means "stopped";
* a ``pw`` change is applied within about two seconds;
* ``pw`` bounds the AC output directly (verified above).

Everything here is pure Python so the policy can be tested against recorded
frames without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .jackery_ble_protocol import SCHEDULE_MODE_DISCHARGE

#: Percentage points to keep above the device's own discharge floor.
DEFAULT_RESERVE_PERCENT = 2

#: Ignore cap changes smaller than this, to keep BLE traffic down.
DEFAULT_MIN_CHANGE_W = 100

#: Grid flow inside this band is treated as balanced; no write is needed.
DEFAULT_GRID_DEADBAND_W = 50

DEFAULT_MAX_SYS_OUT_PW = 2500


class Mode(StrEnum):
    """What the supervisor wants the battery to do."""

    UNKNOWN = "unknown"
    """Inputs are missing; publish nothing and keep the last cap."""

    HOLD = "hold"
    """Publish a zero cap: battery idle, grid serves the house."""

    TRACK = "track"
    """Publish a target: the device outputs that much AC power."""


def vault_output_pw(
    pv_pw: float | None,
    bat_in_pw: float | None,
    bat_out_pw: float | None,
) -> float | None:
    """DC power the vault is delivering to the house, from its own meters.

    ``pvPw``, ``batInPw`` and ``batOutPw`` are real measurements; the grid-side
    fields are not, so they are deliberately excluded. Returns None unless
    every term is known.
    """
    try:
        pv, bat_in, bat_out = (float(value) for value in (pv_pw, bat_in_pw, bat_out_pw))
    except (TypeError, ValueError):
        return None

    return max(0.0, pv + bat_out - bat_in)


@dataclass(frozen=True)
class MeterReading:
    """One consistent snapshot of the fields the policy needs."""

    soc: int | None
    soc_dischg_limit: int | None
    pv_pw: float | None = None
    bat_in_pw: float | None = None
    bat_out_pw: float | None = None
    max_sys_out_pw: int = DEFAULT_MAX_SYS_OUT_PW
    #: Signed power at the utility meter: positive = import, negative = export.
    grid_pw: float | None = None

    @property
    def vault_output_pw(self) -> float | None:
        """Power the vault is putting out; a lower bound on the house load."""
        return vault_output_pw(self.pv_pw, self.bat_in_pw, self.bat_out_pw)


@dataclass(frozen=True)
class Decision:
    """The cap the supervisor should publish, with a reason for the log."""

    mode: Mode
    cap_w: int
    reason: str
    writable: bool = True

    @property
    def discharge(self) -> bool:
        """Whether the battery is allowed to discharge."""
        return self.mode is Mode.TRACK


class FollowMeterPolicy:
    """Decide whether the battery may discharge, and at what output."""

    def __init__(
        self,
        *,
        reserve_percent: int = DEFAULT_RESERVE_PERCENT,
        min_change_w: int = DEFAULT_MIN_CHANGE_W,
        deadband_w: int = DEFAULT_GRID_DEADBAND_W,
    ) -> None:
        """Constructor."""
        self.reserve_percent = max(1, int(reserve_percent))
        self.min_change_w = max(0, int(min_change_w))
        self.deadband_w = max(0, int(deadband_w))

        self._mode = Mode.HOLD
        self._cap_w = 0

    @property
    def mode(self) -> Mode:
        """Current mode."""
        return self._mode

    @property
    def cap_w(self) -> int:
        """Last decided cap."""
        return self._cap_w

    def thresholds(self, floor: int) -> tuple[int, int]:
        """Return ``(start_at, stop_at)`` for a device floor.

        ``start_at`` is one point above the floor by default, so the exclusive
        hardware gate is never the thing that decides to stop.
        """
        start_at = floor + self.reserve_percent
        stop_at = max(floor, start_at - 1)
        return start_at, stop_at

    def observe(self, reading: MeterReading, now: float) -> Decision:
        """Update the mode and return the cap to publish."""
        soc = reading.soc
        floor = reading.soc_dischg_limit

        if soc is None or floor is None:
            # Never publish from an incomplete reading; the device keeps the
            # last cap and still enforces its own floor.
            return Decision(Mode.UNKNOWN, 0, "soc_unknown", writable=False)

        start_at, stop_at = self.thresholds(int(floor))

        if self._mode is Mode.TRACK and soc <= stop_at:
            return self._hold(f"soc {soc} <= stop {stop_at}")
        if self._mode is Mode.HOLD and soc >= start_at:
            self._mode = Mode.TRACK

        if self._mode is Mode.HOLD:
            return self._hold(f"soc {soc} < start {start_at}")

        # Custom-mode ``pw`` is the AC output *target*, not a ceiling: measured
        # 2026-10-23 at 18:23, pw 2500 with a ~570 W house gave batOutPw 2590
        # and -2020 W at the meter. The cap therefore has to be the load, and
        # the only trustworthy load signal is the CT on the grid feed.
        grid = reading.grid_pw
        if grid is None:
            # Without a real grid reading a guess could export; stop instead.
            return self._hold("no grid measurement")

        if abs(grid) <= self.deadband_w:
            # Output already matches the load; leave the plan alone.
            return Decision(
                Mode.TRACK,
                self._cap_w,
                f"grid {grid:.0f} W within {self.deadband_w:.0f} W",
            )

        max_cap = max(0, int(reading.max_sys_out_pw))
        # One watt of grid import is one watt of output still missing, so the
        # correction is the measured error itself. Driving the grid to zero
        # also absorbs whatever the PV systems on the same feed are doing.
        cap = min(max_cap, max(0, round(self._cap_w + grid)))
        self._cap_w = cap
        return Decision(Mode.TRACK, cap, f"grid {grid:.0f} W -> {cap} W")

    def needs_write(self, decision: Decision, last_sent: Decision | None) -> bool:
        """Whether ``decision`` differs enough from ``last_sent`` to be sent."""
        if not decision.writable:
            return False
        if last_sent is None:
            return True
        if decision.mode is not last_sent.mode:
            return True
        return abs(decision.cap_w - last_sent.cap_w) >= self.min_change_w

    def _hold(self, reason: str) -> Decision:
        """Enter HOLD and return a zero cap."""
        self._mode = Mode.HOLD
        self._cap_w = 0
        return Decision(Mode.HOLD, 0, reason)


def parse_hhmm(value: str) -> int:
    """Parse ``"HH:mm"`` into minutes past midnight."""
    hours, _, minutes = value.partition(":")
    return int(hours) * 60 + int(minutes)


def _window_contains(start: str, end: str, now: datetime) -> bool:
    """Whether ``now`` falls inside a schedule window.

    Windows ending at or before their start run overnight (e.g. 22:00-06:00).
    """
    start_minutes = parse_hhmm(start)
    end_minutes = parse_hhmm(end)
    now_minutes = now.hour * 60 + now.minute

    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def task_is_active(task: dict, now: datetime) -> bool:
    """Whether a ``3018/112`` task applies right now.

    ``loops`` is seven flags, Monday first, matching ``datetime.weekday()``.
    """
    if task.get("mode") != SCHEDULE_MODE_DISCHARGE:
        return False
    if task.get("sysSwitch", 1) != 1:
        return False

    loops = str(task.get("loops", "1111111"))
    if len(loops) != 7:
        return False
    if loops[now.weekday()] != "1":
        return False

    return _window_contains(
        str(task.get("start", "00:00")), str(task.get("end", "24:00")), now
    )


def select_active_task(tasks: list[dict], now: datetime) -> dict | None:
    """Return the discharge task that currently applies, if any."""
    for task in tasks:
        if task_is_active(task, now):
            return task
    return None
