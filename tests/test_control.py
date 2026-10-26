"""Tests for the follow-meter policy and schedule selection.

The policy is pure Python: no Home Assistant runtime, no device. These tests
pin the rules that were established on the real hardware, especially the
exclusive discharge floor.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.jackery_ble.control import (
    Decision,
    FollowMeterPolicy,
    MeterReading,
    Mode,
    parse_hhmm,
    select_active_task,
    task_is_active,
)


def _reading(**overrides) -> MeterReading:
    """A healthy reading: 500 W of PV, 300 W from the battery, 500 W import."""
    values = {
        "soc": 35,
        "soc_dischg_limit": 33,
        "pv_pw": 500.0,
        "bat_in_pw": 0.0,
        "bat_out_pw": 300.0,
        "max_sys_out_pw": 2500,
        "grid_pw": 500.0,
    }
    values.update(overrides)
    return MeterReading(**values)


def test_vault_output_is_pv_plus_discharge_minus_charge() -> None:
    """The grid-side fields are plan-derived, so they are excluded."""
    assert _reading().vault_output_pw == 800.0

    charging = _reading(bat_in_pw=400.0, bat_out_pw=0.0)
    assert charging.vault_output_pw == 100.0

    idle = _reading(pv_pw=0.0, bat_out_pw=0.0)
    assert idle.vault_output_pw == 0.0


def test_vault_output_is_unknown_when_a_field_is_missing() -> None:
    """A partial frame must not produce a fabricated figure."""
    assert _reading(pv_pw=None).vault_output_pw is None
    assert _reading(bat_in_pw=None).vault_output_pw is None


def test_missing_soc_holds_the_battery() -> None:
    """Without a fresh SOC nothing may be published at all."""
    policy = FollowMeterPolicy()

    decision = policy.observe(_reading(soc=None), now=0.0)

    assert decision.mode is Mode.UNKNOWN
    assert decision.cap_w == 0
    assert decision.writable is False
    assert policy.needs_write(decision, None) is False


def test_missing_floor_holds_the_battery() -> None:
    """Without the device floor the reserve cannot be computed."""
    policy = FollowMeterPolicy()

    decision = policy.observe(_reading(soc_dischg_limit=None), now=0.0)

    assert decision.mode is Mode.UNKNOWN
    assert decision.writable is False


def test_below_reserve_holds_with_a_zero_cap() -> None:
    """At 34 % with a floor of 33 the default reserve of 2 keeps it idle."""
    policy = FollowMeterPolicy(reserve_percent=2)

    decision = policy.observe(_reading(soc=34), now=0.0)

    assert decision.mode is Mode.HOLD
    assert decision.cap_w == 0


def test_at_reserve_tracks_the_measured_import() -> None:
    """The plan's pw is the output target, so it must match the import."""
    policy = FollowMeterPolicy(reserve_percent=2)

    decision = policy.observe(_reading(soc=35, grid_pw=500.0), now=0.0)

    assert decision.mode is Mode.TRACK
    assert decision.cap_w == 500


def test_tracking_stops_one_point_above_the_floor() -> None:
    """Once tracking, a drop to the stop threshold ends discharge."""
    policy = FollowMeterPolicy(reserve_percent=2)
    assert policy.observe(_reading(soc=35), now=0.0).mode is Mode.TRACK

    still_tracking = policy.observe(_reading(soc=35), now=10.0)
    assert still_tracking.mode is Mode.TRACK

    stopping = policy.observe(_reading(soc=34), now=20.0)
    assert stopping.mode is Mode.HOLD
    assert stopping.cap_w == 0


def test_hysteresis_prevents_restart_at_the_stop_threshold() -> None:
    """After stopping at 34 it takes 35 again, not 34, to resume."""
    policy = FollowMeterPolicy(reserve_percent=2)
    policy.observe(_reading(soc=35), now=0.0)
    policy.observe(_reading(soc=34), now=10.0)

    assert policy.observe(_reading(soc=34), now=20.0).mode is Mode.HOLD
    assert policy.observe(_reading(soc=35), now=30.0).mode is Mode.TRACK


def test_minimum_reserve_never_sits_on_the_exclusive_floor() -> None:
    """Reserve 1 still starts above the floor the device refuses to cross."""
    policy = FollowMeterPolicy(reserve_percent=1)

    assert policy.thresholds(33) == (34, 33)
    assert policy.observe(_reading(soc=33), now=0.0).mode is Mode.HOLD
    assert policy.observe(_reading(soc=34), now=10.0).mode is Mode.TRACK


def test_export_lowers_the_target() -> None:
    """2 kW of export while targeting 2.5 kW leaves a 500 W load."""
    policy = FollowMeterPolicy()
    policy.observe(_reading(grid_pw=2500.0), now=0.0)

    decision = policy.observe(_reading(grid_pw=-2000.0), now=10.0)

    assert decision.mode is Mode.TRACK
    assert decision.cap_w == 500


def test_import_raises_the_target() -> None:
    """Import is output still missing, so it is added to the target."""
    policy = FollowMeterPolicy()
    policy.observe(_reading(grid_pw=400.0), now=0.0)

    decision = policy.observe(_reading(grid_pw=300.0), now=10.0)

    assert decision.cap_w == 700


def test_deadband_leaves_the_target_alone() -> None:
    """Noise inside the deadband must not become BLE traffic."""
    policy = FollowMeterPolicy()
    policy.observe(_reading(grid_pw=600.0), now=0.0)

    decision = policy.observe(_reading(grid_pw=30.0), now=10.0)

    assert decision.cap_w == 600
    assert "within" in decision.reason


def test_missing_grid_holds_the_battery() -> None:
    """No real grid reading means no guess: exporting is the worse failure."""
    policy = FollowMeterPolicy()

    decision = policy.observe(_reading(grid_pw=None), now=0.0)

    assert decision.mode is Mode.HOLD
    assert decision.cap_w == 0


def test_cap_is_clamped_to_the_device_maximum() -> None:
    """A huge import must not ask for more than the inverter can give."""
    policy = FollowMeterPolicy()

    decision = policy.observe(
        _reading(grid_pw=4000.0, max_sys_out_pw=1800), now=0.0
    )

    assert decision.cap_w == 1800


def test_zero_device_limit_publishes_a_zero_target() -> None:
    """A device reporting 0 W must not be given a made-up output."""
    policy = FollowMeterPolicy()

    decision = policy.observe(_reading(grid_pw=500.0, max_sys_out_pw=0), now=0.0)

    assert decision.cap_w == 0


def test_needs_write_ignores_small_cap_changes() -> None:
    """Small load wobble should not turn into BLE traffic."""
    policy = FollowMeterPolicy(min_change_w=100)
    first = Decision(Mode.TRACK, 1000, "")
    small = Decision(Mode.TRACK, 1050, "")
    big = Decision(Mode.TRACK, 1100, "")

    assert policy.needs_write(first, None) is True
    assert policy.needs_write(small, first) is False
    assert policy.needs_write(big, first) is True


def test_needs_write_always_sends_a_mode_change() -> None:
    """Stopping must never be skipped because the cap looks similar."""
    policy = FollowMeterPolicy(min_change_w=500)

    assert policy.needs_write(Decision(Mode.HOLD, 0, ""), Decision(Mode.TRACK, 0, ""))


def test_parse_hhmm_accepts_midnight_end() -> None:
    """The vault uses 24:00 for end of day."""
    assert parse_hhmm("08:00") == 480
    assert parse_hhmm("24:00") == 1440
    assert parse_hhmm("23:59") == 1439


def _task(**overrides) -> dict:
    task = {
        "tid": "1778312607",
        "loops": "1111111",
        "start": "08:00",
        "end": "19:00",
        "mode": 2,
        "pw": 1010,
        "sysSwitch": 1,
    }
    task.update(overrides)
    return task


def test_active_task_matches_a_daytime_window() -> None:
    """Friday noon is inside the 08:00-19:00 plan."""
    assert task_is_active(_task(), datetime(2026, 10, 23, 12, 0)) is True


def test_inactive_outside_the_window() -> None:
    """20:00 is outside the 08:00-19:00 plan."""
    assert task_is_active(_task(), datetime(2026, 10, 23, 20, 0)) is False


def test_overnight_window_wraps_around_midnight() -> None:
    """22:00-06:00 applies on both sides of midnight."""
    overnight = _task(start="22:00", end="06:00")

    assert task_is_active(overnight, datetime(2026, 10, 23, 23, 0)) is True
    assert task_is_active(overnight, datetime(2026, 10, 23, 5, 0)) is True
    assert task_is_active(overnight, datetime(2026, 10, 23, 12, 0)) is False


def test_day_flags_are_monday_first() -> None:
    """A Monday-only plan must not fire on Friday."""
    monday_only = _task(loops="1000000")
    friday = datetime(2026, 10, 23, 12, 0)

    assert task_is_active(monday_only, friday) is False
    assert task_is_active(monday_only, datetime(2026, 10, 19, 12, 0)) is True


def test_charge_plans_and_switched_off_plans_are_ignored() -> None:
    """Only enabled discharge plans may be used as a cap target."""
    now = datetime(2026, 10, 23, 12, 0)

    assert task_is_active(_task(mode=1), now) is False
    assert task_is_active(_task(sysSwitch=0), now) is False


def test_select_active_task_picks_the_matching_plan() -> None:
    """The first applicable plan wins."""
    tasks = [
        _task(tid="night", start="01:00", end="07:00", pw=2120),
        _task(tid="day", start="08:00", end="19:00", pw=1010),
    ]

    selected = select_active_task(tasks, datetime(2026, 10, 23, 12, 0))

    assert selected is not None
    assert selected["tid"] == "day"


def test_select_active_task_returns_none_in_a_gap() -> None:
    """Gaps between plans must be reported, not silently filled."""
    tasks = [_task(start="08:00", end="19:00")]

    assert select_active_task(tasks, datetime(2026, 10, 23, 7, 30)) is None
