"""Supervisor tests with a fake coordinator.

The supervisor owns the *decision to write*: cooldowns, plan selection and the
safety rule that nothing is published without fresh data and a known floor.
The write path itself is exercised against the real command builders.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.jackery_ble.jackery_ble_protocol import JackeryBleCommands
from custom_components.jackery_ble.supervisor import FollowMeterSupervisor

NOON = datetime(2026, 10, 23, 12, 0)

DAY_PLAN = {
    "tid": "1778312607",
    "loops": "1111111",
    "start": "08:00",
    "end": "19:00",
    "mode": 2,
    "pw": 1010,
    "sysSwitch": 1,
}

NIGHT_PLAN = {
    "tid": "1778312975",
    "loops": "1111111",
    "start": "01:00",
    "end": "07:00",
    "mode": 2,
    "pw": 2120,
    "sysSwitch": 1,
}

CHARGE_PLAN = {
    "tid": "1778313001",
    "loops": "1111111",
    "start": "10:00",
    "end": "16:00",
    "mode": 1,
    "pw": 800,
    "sysSwitch": 1,
}


def _state(**overrides) -> dict:
    """A merged push state that the policy can act on."""
    state = {
        "soc": 35,
        "socDischgLimit": 33,
        "pvPw": 500,
        "batInPw": 0,
        "batOutPw": 300,
        "maxSysOutPw": 2500,
    }
    state.update(overrides)
    return state


class FakeCoordinator:
    """Minimal coordinator surface used by the supervisor."""

    def __init__(self, device_data, *, data=None, tasks=None, connected=True):
        self.data = data if data is not None else _state()
        self.tasks = tasks if tasks is not None else [DAY_PLAN, NIGHT_PLAN]
        self.connected = connected
        self.commands = JackeryBleCommands(device_data)
        self.sent = []
        self.schedule_reads = 0

    def available(self) -> bool:
        """Report link health."""
        return self.connected

    async def async_read_schedule(self, max_wait: float = 5.0):
        """Return the fake plan list."""
        self.schedule_reads += 1
        return self.tasks

    async def async_send_command(self, command) -> None:
        """Record the write."""
        self.sent.append(command)


GRID_ENTITY = "sensor.ct_grid_power"


def _supervisor(hass, coordinator, grid_w=500) -> FollowMeterSupervisor:
    hass.states.async_set(GRID_ENTITY, str(grid_w))
    return FollowMeterSupervisor(
        hass, coordinator, grid_power_entity_id=GRID_ENTITY
    )


def _caps(coordinator, tid: str = DAY_PLAN["tid"]) -> list:
    """Schedule writes that target one plan (the active one by default)."""
    return [
        command
        for command in coordinator.sent
        if command.action_id == 3017 and command.body.get("tid") == tid
    ]


async def test_tracking_writes_the_measured_load(hass, home_device_data) -> None:
    """At floor + reserve the plan target is the measured grid import."""
    coordinator = FakeCoordinator(home_device_data)
    supervisor = _supervisor(hass, coordinator, grid_w=500)

    await supervisor._async_evaluate(NOON)

    assert len(_caps(coordinator)) == 1
    command = _caps(coordinator)[0]
    assert command.action_id == 3017
    assert command.body["tid"] == DAY_PLAN["tid"]
    assert command.body["pw"] == 500
    assert command.body["taskType"] == 2


async def test_hold_publishes_a_zero_cap(hass, home_device_data) -> None:
    """At the reserve boundary the plan is capped to nothing."""
    coordinator = FakeCoordinator(home_device_data, data=_state(soc=34))
    supervisor = _supervisor(hass, coordinator)

    await supervisor._async_evaluate(NOON)

    assert len(_caps(coordinator)) == 1
    assert _caps(coordinator)[0].body["pw"] == 0


async def test_no_write_without_a_matching_plan(hass, home_device_data) -> None:
    """No active plan means no target, but stored ones are still zeroed."""
    coordinator = FakeCoordinator(home_device_data, tasks=[NIGHT_PLAN])
    supervisor = _supervisor(hass, coordinator)

    await supervisor._async_evaluate(NOON)

    assert _caps(coordinator) == []
    night = _caps(coordinator, NIGHT_PLAN["tid"])
    assert [command.body["pw"] for command in night] == [0]


async def test_no_write_while_the_link_is_unavailable(hass, home_device_data) -> None:
    """Stale data means the last cap stays, not a new guess."""
    coordinator = FakeCoordinator(home_device_data, connected=False)
    supervisor = _supervisor(hass, coordinator)

    await supervisor._async_evaluate(NOON)

    assert _caps(coordinator) == []


async def test_unknown_soc_publishes_nothing(hass, home_device_data) -> None:
    """Without a SOC the reserve cannot be evaluated."""
    coordinator = FakeCoordinator(home_device_data, data=_state(soc=None))
    supervisor = _supervisor(hass, coordinator)

    await supervisor._async_evaluate(NOON)

    assert _caps(coordinator) == []


async def test_stable_target_is_written_once(hass, home_device_data) -> None:
    """A balanced grid must not become periodic BLE traffic."""
    coordinator = FakeCoordinator(home_device_data)
    supervisor = _supervisor(hass, coordinator, grid_w=500)
    await supervisor._async_evaluate(NOON)

    hass.states.async_set(GRID_ENTITY, "20")
    await supervisor._async_evaluate(datetime(2026, 10, 23, 12, 1))

    assert len(_caps(coordinator)) == 1


async def test_write_cooldown_is_respected(hass, home_device_data) -> None:
    """A large change inside the cooldown waits for the next allowed write."""
    coordinator = FakeCoordinator(home_device_data)
    supervisor = _supervisor(hass, coordinator, grid_w=500)
    await supervisor._async_evaluate(NOON)

    hass.states.async_set(GRID_ENTITY, "-1500")
    await supervisor._async_evaluate(NOON + timedelta(seconds=2))

    assert len(_caps(coordinator)) == 1

    await supervisor._async_evaluate(NOON + timedelta(seconds=10))

    assert len(_caps(coordinator)) == 2
    assert _caps(coordinator)[1].body["pw"] == 0


async def test_failed_schedule_read_keeps_the_known_plans(
    hass, home_device_data
) -> None:
    """A timed-out read must not wipe the plans already known."""
    coordinator = FakeCoordinator(home_device_data)
    supervisor = _supervisor(hass, coordinator)
    await supervisor._async_evaluate(NOON)

    coordinator.tasks = None
    supervisor._last_schedule_read = None
    await supervisor._async_evaluate(datetime(2026, 10, 23, 12, 1))

    assert [task["tid"] for task in supervisor._tasks] == [
        DAY_PLAN["tid"],
        NIGHT_PLAN["tid"],
    ]


async def test_target_never_exceeds_the_device_rating(hass, home_device_data) -> None:
    """A 4 kW import must still be capped at maxSysOutPw."""
    coordinator = FakeCoordinator(
        home_device_data,
        data=_state(maxSysOutPw=2500),
    )
    supervisor = _supervisor(hass, coordinator, grid_w=4000)

    await supervisor._async_evaluate(NOON)

    assert _caps(coordinator)[0].body["pw"] == 2500


async def test_refresh_zeroes_only_the_inactive_plans(
    hass, home_device_data
) -> None:
    """Only plans HA is not writing are zeroed, so a future window is safe."""
    coordinator = FakeCoordinator(home_device_data)
    supervisor = _supervisor(hass, coordinator, grid_w=500)

    await supervisor._async_evaluate(NOON)

    night = _caps(coordinator, NIGHT_PLAN["tid"])
    assert [command.body["pw"] for command in night] == [0]
    assert [command.body["pw"] for command in _caps(coordinator)] == [500]


async def test_starting_zeroes_every_stored_plan(hass, home_device_data) -> None:
    """A restart must not leave any plan at a target that can drain the battery."""
    coordinator = FakeCoordinator(home_device_data)
    supervisor = _supervisor(hass, coordinator, grid_w=500)

    await supervisor.async_start()
    await supervisor.async_stop()

    assert [c.body["pw"] for c in _caps(coordinator)] == [0]
    assert [c.body["pw"] for c in _caps(coordinator, NIGHT_PLAN["tid"])] == [0]


async def test_zeroed_plans_are_not_rewritten_every_refresh(
    hass, home_device_data
) -> None:
    """Zeroing is a one-off per plan, not 10-minute BLE traffic."""
    coordinator = FakeCoordinator(home_device_data)
    supervisor = _supervisor(hass, coordinator, grid_w=500)
    await supervisor._async_evaluate(NOON)
    night_writes = len(_caps(coordinator, NIGHT_PLAN["tid"]))

    supervisor._last_schedule_read = None
    await supervisor._async_evaluate(datetime(2026, 10, 23, 12, 1))

    assert len(_caps(coordinator, NIGHT_PLAN["tid"])) == night_writes


async def test_charge_plans_are_left_alone(hass, home_device_data) -> None:
    """Only discharge targets are dangerous; charging schedules keep their pw."""
    coordinator = FakeCoordinator(
        home_device_data, tasks=[DAY_PLAN, NIGHT_PLAN, CHARGE_PLAN]
    )
    supervisor = _supervisor(hass, coordinator, grid_w=500)

    await supervisor.async_start()
    await supervisor.async_stop()

    assert _caps(coordinator, CHARGE_PLAN["tid"]) == []
    assert [c.body["pw"] for c in _caps(coordinator, NIGHT_PLAN["tid"])] == [0]
