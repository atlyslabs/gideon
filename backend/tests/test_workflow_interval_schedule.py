"""Unit tests for the workflow scheduler's interval (minute/hour) support.

`_is_workflow_due` and `_interval_seconds` are pure (no DB), so they're tested
directly with lightweight stand-in workflow objects. The DB-backed dedup
(`_last_scheduled_runs`) is exercised by the integration suite.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from services.scheduler import _interval_seconds, _is_workflow_due


def _wf(**overrides):
    """A minimal stand-in carrying just the attributes `_is_workflow_due` reads."""
    base = dict(
        schedule_interval=1,
        schedule_unit="day",
        schedule_start_date=None,
        schedule_day_of_week=None,
        schedule_days_of_week=None,
        schedule_time="09:00",
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# A fixed "now" — 14:37 UTC on a Wednesday (2026-06-03).
NOW = datetime(2026, 6, 3, 14, 37, tzinfo=timezone.utc)


def test_interval_seconds_mapping():
    assert _interval_seconds(_wf(schedule_unit="minute", schedule_interval=30)) == 1800
    assert _interval_seconds(_wf(schedule_unit="hour", schedule_interval=2)) == 7200
    assert _interval_seconds(_wf(schedule_unit="day")) is None
    assert _interval_seconds(_wf(schedule_unit="week")) is None


def test_minute_interval_is_due_regardless_of_time():
    # No time-of-day anchor: eligible on any tick once start-date gating passes.
    wf = _wf(schedule_unit="minute", schedule_interval=15)
    assert _is_workflow_due(wf, NOW, "UTC") is True
    # A different wall-clock minute is still eligible (cadence is handled by the
    # elapsed-since-last-run DB check, not by this function).
    assert _is_workflow_due(wf, NOW + timedelta(minutes=3), "UTC") is True


def test_hour_interval_respects_future_start_date():
    wf = _wf(schedule_unit="hour", schedule_interval=2, schedule_start_date=date(2026, 6, 10))
    assert _is_workflow_due(wf, NOW, "UTC") is False


def test_interval_respects_weekday_filter():
    # NOW is a Wednesday. Filter allows only Mon/Tue -> not due.
    wf = _wf(schedule_unit="minute", schedule_interval=30, schedule_days_of_week=["MO", "TU"])
    assert _is_workflow_due(wf, NOW, "UTC") is False
    # Filter includes Wednesday -> due.
    wf_ok = _wf(schedule_unit="minute", schedule_interval=30, schedule_days_of_week=["WE"])
    assert _is_workflow_due(wf_ok, NOW, "UTC") is True


def test_daily_time_window_still_works():
    # Regression: day-unit schedules still match only inside the HH:MM window.
    on_time = _wf(schedule_unit="day", schedule_time="14:37")
    assert _is_workflow_due(on_time, NOW, "UTC") is True
    off_time = _wf(schedule_unit="day", schedule_time="08:00")
    assert _is_workflow_due(off_time, NOW, "UTC") is False
