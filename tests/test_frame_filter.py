"""Duplicate suppression and liveness tests for inbound BLE frames."""

from __future__ import annotations

from custom_components.jackery_ble.frame_filter import (
    DUPLICATE_WINDOW,
    STALE_AFTER,
    FrameFilter,
)


def test_first_frame_is_always_accepted() -> None:
    """Nothing is known yet, so the first frame must reach the coordinator."""
    frame_filter = FrameFilter()

    assert frame_filter.accept(b"\xdf\xed\x01", now=100.0)
    assert frame_filter.last_seen == 100.0


def test_byte_identical_frame_inside_window_is_dropped() -> None:
    """The devices repeat every notification about 405 ms later."""
    frame_filter = FrameFilter()
    frame_filter.accept(b"\xdf\xed\x01", now=100.0)

    assert not frame_filter.accept(b"\xdf\xed\x01", now=100.405)


def test_duplicate_refreshes_liveness() -> None:
    """A dropped duplicate still proves the device is alive."""
    frame_filter = FrameFilter()
    frame_filter.accept(b"\xdf\xed\x01", now=100.0)

    frame_filter.accept(b"\xdf\xed\x01", now=100.405)

    assert frame_filter.last_seen == 100.405


def test_static_reading_is_not_suppressed_forever() -> None:
    """A repeated payload must resurface on the next push cadence."""
    frame_filter = FrameFilter()
    frame_filter.accept(b"\xdf\xed\x01", now=100.0)
    frame_filter.accept(b"\xdf\xed\x01", now=100.405)

    assert frame_filter.accept(b"\xdf\xed\x01", now=102.4)


def test_changed_payload_is_accepted_immediately() -> None:
    """Different bytes are new information, however close together."""
    frame_filter = FrameFilter()
    frame_filter.accept(b"\xdf\xed\x01", now=100.0)

    assert frame_filter.accept(b"\xdf\xed\x02", now=100.1)


def test_window_boundary_is_exclusive() -> None:
    """A frame exactly at the window edge counts as new again."""
    frame_filter = FrameFilter()
    frame_filter.accept(b"\xdf\xed\x01", now=100.0)

    assert not frame_filter.accept(b"\xdf\xed\x01", now=100.0 + DUPLICATE_WINDOW - 0.001)
    assert frame_filter.accept(b"\xdf\xed\x01", now=100.0 + DUPLICATE_WINDOW)


def test_staleness_is_false_before_the_first_frame() -> None:
    """Startup must not report stale data before anything has arrived."""
    frame_filter = FrameFilter()

    assert not frame_filter.is_stale(now=10_000.0)


def test_staleness_after_the_grace_period() -> None:
    """Entities go unavailable only after the CT stops pushing entirely."""
    frame_filter = FrameFilter()
    frame_filter.accept(b"\xdf\xed\x01", now=100.0)

    assert not frame_filter.is_stale(now=100.0 + STALE_AFTER)
    assert frame_filter.is_stale(now=100.0 + STALE_AFTER + 0.001)
