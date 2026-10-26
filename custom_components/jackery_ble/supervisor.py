"""Slow outer loop that publishes a discharge cap to the SolarVault.

The vault already balances PV, battery and grid on its own; this supervisor
only decides *whether* the battery may discharge and what ceiling to publish.
See ``control.py`` for the decision logic and the local development notes for
the device behaviour it is built on.

Safety model:

* Nothing is written until fresh push data and the discharge floor are known.
* A stale link means no writes at all - the device keeps the last cap it was
  given, and its own ``socDischgLimit`` check keeps stopping discharge.
* The published cap never exceeds the device's ``maxSysOutPw``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .control import (
    DEFAULT_MAX_SYS_OUT_PW,
    DEFAULT_RESERVE_PERCENT,
    Decision,
    FollowMeterPolicy,
    MeterReading,
    select_active_task,
)
from .coordinator import JackeryBleCoordinator
from .jackery_ble_protocol import SCHEDULE_MODE_DISCHARGE, ScheduleTask

_LOGGER = logging.getLogger(__name__)

#: How often the policy is evaluated.
TICK = timedelta(seconds=10)

#: Minimum spacing between two cap writes. The loop needs to react within one
#: tick: while the target is above the load the excess goes to the grid.
WRITE_COOLDOWN = timedelta(seconds=5)

#: How often the schedule is re-read (the user may edit plans on the device).
SCHEDULE_REFRESH = timedelta(minutes=10)

#: How long to wait for a ``3018/112`` reply.
SCHEDULE_TIMEOUT = 5.0

#: A grid reading older than this is treated as missing; the policy then stops
#: discharging rather than guessing an output.
GRID_STALE = timedelta(seconds=60)


def _as_float(value) -> float | None:
    """Return ``value`` as a float, or None when it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    """Return ``value`` as an int, or None when it is not a number."""
    number = _as_float(value)
    return None if number is None else int(number)


class FollowMeterSupervisor:
    """Decide and publish the SolarVault's discharge cap."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: JackeryBleCoordinator,
        *,
        reserve_percent: int = DEFAULT_RESERVE_PERCENT,
        grid_power_entity_id: str | None = None,
    ) -> None:
        """Constructor."""
        self.hass = hass
        self.coordinator = coordinator
        self.policy = FollowMeterPolicy(reserve_percent=reserve_percent)
        self._grid_entity_id = grid_power_entity_id

        self._tasks: list[dict] = []
        self._last_sent: Decision | None = None
        self._last_write: datetime | None = None
        self._last_schedule_read: datetime | None = None
        self._unsub = None
        self._warned_no_task = False
        self._warned_unknown = False
        self._warned_no_grid = False

    async def async_start(self) -> None:
        """Read the schedule, zero stale targets, and start the tick."""
        await self._async_refresh_schedule(dt_util.now(), zero_all=True)
        self._unsub = async_track_time_interval(self.hass, self._handle_tick, TICK)
        _LOGGER.info(
            "Follow-meter supervisor started (reserve %s%%)",
            self.policy.reserve_percent,
        )

    async def async_stop(self) -> None:
        """Stop the tick. The device keeps the last published cap."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        _LOGGER.info("Follow-meter supervisor stopped")

    @callback
    def _handle_tick(self, now: datetime) -> None:
        """Schedule one evaluation without blocking the timer callback."""
        self.hass.async_create_task(self._async_evaluate(now))

    async def _async_evaluate(self, now: datetime) -> None:
        """Evaluate the policy and write a new cap when it changed enough."""
        if not self.coordinator.available():
            _LOGGER.debug("Skipping: device is unavailable")
            return

        if (
            self._last_schedule_read is None
            or now - self._last_schedule_read > SCHEDULE_REFRESH
        ):
            await self._async_refresh_schedule(now)

        reading = self._reading()
        if reading.grid_pw is None and not self._warned_no_grid:
            _LOGGER.warning(
                "No fresh grid power reading; the battery is held at 0 W so it "
                "cannot export. Configure the CT (Smart Meter) entry."
            )
            self._warned_no_grid = True
        elif reading.grid_pw is not None:
            self._warned_no_grid = False

        decision = self.policy.observe(reading, now.timestamp())

        if not decision.writable:
            if not self._warned_unknown:
                _LOGGER.info(
                    "Waiting for SOC and the discharge floor before publishing "
                    "a cap (soc %s, floor %s)",
                    reading.soc,
                    reading.soc_dischg_limit,
                )
                self._warned_unknown = True
            return

        self._warned_unknown = False

        if not self.policy.needs_write(decision, self._last_sent):
            return

        if (
            self._last_write is not None
            and now - self._last_write < WRITE_COOLDOWN
        ):
            return

        task = select_active_task(self._tasks, now)
        if task is None:
            if not self._warned_no_task:
                _LOGGER.warning(
                    "No matching discharge plan is active; publish nothing. "
                    "Plans: %s",
                    self._tasks,
                )
                self._warned_no_task = True
            return

        plan = ScheduleTask.from_body(task, pw=decision.cap_w)
        try:
            await self.coordinator.async_send_command(
                self.coordinator.commands.update_schedule_task(plan)
            )
        except Exception:  # a failed write must not kill the tick
            _LOGGER.exception("Could not publish cap %s W", decision.cap_w)
            return

        self._last_sent = decision
        self._last_write = now
        self._warned_no_task = False
        _LOGGER.info(
            "Cap -> %s W on plan %s (%s, soc %s, floor %s, output %s W, "
            "grid %s W)",
            decision.cap_w,
            plan.tid,
            decision.reason,
            reading.soc,
            reading.soc_dischg_limit,
            reading.vault_output_pw,
            reading.grid_pw,
        )

    def _reading(self) -> MeterReading:
        """Build a policy input from the coordinator's merged push data."""
        data = self.coordinator.data or {}

        return MeterReading(
            soc=_as_int(data.get("soc")),
            soc_dischg_limit=_as_int(data.get("socDischgLimit")),
            pv_pw=_as_float(data.get("pvPw")),
            bat_in_pw=_as_float(data.get("batInPw")),
            bat_out_pw=_as_float(data.get("batOutPw")),
            max_sys_out_pw=_as_int(data.get("maxSysOutPw"))
            or DEFAULT_MAX_SYS_OUT_PW,
            grid_pw=self._grid_pw(),
        )

    def _grid_pw(self) -> float | None:
        """Signed power at the utility meter, or None when it is not usable."""
        entity_id = self._grid_entity_id or self._resolve_grid_entity()
        if entity_id is None:
            return None

        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None

        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None

        if dt_util.utcnow() - state.last_updated > GRID_STALE:
            return None
        return value

    def _resolve_grid_entity(self) -> str | None:
        """Find the CT's power sensor through the accessory config entry.

        The Smart Meter is a separate device with its own entry; its power
        sensor is the only real measurement of grid flow in the system.
        """
        registry = er.async_get(self.hass)
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            runtime = getattr(entry, "runtime_data", None)
            device_data = getattr(runtime, "device_data", None)
            if device_data is None or not device_data.is_accessory:
                continue

            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{device_data.sn}_power"
            )
            if entity_id is not None:
                self._grid_entity_id = entity_id
                _LOGGER.info("Grid feedback from %s", entity_id)
                return entity_id

        return None

    async def _async_refresh_schedule(
        self, now: datetime, *, zero_all: bool = False
    ) -> None:
        """Ask the device for its plans and clear targets we do not manage.

        A plan's ``pw`` is what the device falls back to when HA stops
        supervising, so every plan that is not the one being written right now
        is left at 0 W. Otherwise a future window (e.g. 19:00-24:00 at 2360 W)
        can drain the battery during a BLE outage.
        """
        tasks = await self.coordinator.async_read_schedule(SCHEDULE_TIMEOUT)
        self._last_schedule_read = now

        # The discharge floor is only reported by 3011/106, so refresh it on
        # the same cadence: the user may have moved it on the device.
        await self.coordinator.async_send_command(
            self.coordinator.commands.read_device_info()
        )

        if tasks is None:
            _LOGGER.debug(
                "Schedule read timed out; keeping %d plan(s)", len(self._tasks)
            )
            return

        self._tasks = tasks
        _LOGGER.debug("Schedule now has %d plan(s)", len(tasks))

        active = select_active_task(self._tasks, now)
        keep_tid = None
        if not zero_all and active is not None:
            keep_tid = str(active.get("tid"))
        await self._async_zero_plans(keep_tid)

    async def _async_zero_plans(self, keep_tid: str | None) -> None:
        """Set every stored target to 0 W except the plan HA is managing."""
        zeroed = 0
        for index, task in enumerate(self._tasks):
            if task.get("mode") != SCHEDULE_MODE_DISCHARGE:
                # Charge plans cannot over-discharge; leave them alone.
                continue

            tid = str(task.get("tid"))
            if tid == keep_tid:
                continue

            try:
                pw = int(task.get("pw", 0))
            except (TypeError, ValueError):
                pw = 0
            if pw == 0:
                continue

            plan = ScheduleTask.from_body(task, pw=0)
            try:
                await self.coordinator.async_send_command(
                    self.coordinator.commands.update_schedule_task(plan)
                )
            except Exception:
                _LOGGER.exception("Could not zero the target of plan %s", tid)
                continue

            # Replace the entry rather than mutating the caller's dict; the
            # next refresh then sees the plan as already zeroed.
            self._tasks[index] = {**task, "pw": 0}
            zeroed += 1

        if zeroed:
            _LOGGER.info(
                "Zeroed %d stored plan target(s): a link loss cannot discharge",
                zeroed,
            )
