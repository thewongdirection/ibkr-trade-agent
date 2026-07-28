"""Tests for run-cadence resolution (agent/schedule.py)."""

from __future__ import annotations

from datetime import date

import pytest

from agent.schedule import ScheduleError, resolve_schedule

# Reference dates with a known DST state for America/New_York.
EDT = date(2026, 7, 15)  # summer -> UTC-4
EST = date(2026, 1, 15)  # winter -> UTC-5


def test_daily_default_weekday_cron_edt():
    plan = resolve_schedule({"frequency": "daily", "run_time": "08:30"}, reference_date=EDT)
    assert plan.cron_utc == "30 12 * * 1-5"
    assert plan.needs_run_gate is False


def test_daily_dst_shift_est():
    plan = resolve_schedule({"frequency": "daily", "run_time": "08:30"}, reference_date=EST)
    assert plan.cron_utc == "30 13 * * 1-5"  # EST pushes an hour later in UTC


def test_weekly_uses_day_of_week():
    plan = resolve_schedule(
        {"frequency": "weekly", "run_time": "08:30", "day_of_week": "wed"},
        reference_date=EDT,
    )
    assert plan.cron_utc == "30 12 * * 3"  # Wed
    assert plan.needs_run_gate is False


def test_monthly_uses_day_of_month():
    plan = resolve_schedule(
        {"frequency": "monthly", "run_time": "08:30", "day_of_month": 5}, reference_date=EDT
    )
    assert plan.cron_utc == "30 12 5 * *"


def test_biweekly_needs_gate_and_alternates_weeks():
    plan = resolve_schedule(
        {"frequency": "biweekly", "run_time": "08:30", "day_of_week": "mon"},
        reference_date=EDT,
    )
    assert plan.cron_utc == "30 12 * * 1"
    assert plan.needs_run_gate is True
    even_week = date(2026, 1, 12)  # ISO week 3 (odd) -> skip
    other_week = date(2026, 1, 5)  # ISO week 2 (even) -> run
    assert plan.should_run_on(other_week) is True
    assert plan.should_run_on(even_week) is False


def test_explicit_cron_override_wins():
    plan = resolve_schedule(
        {"frequency": "daily", "cron": "0 6 * * 1-5"}, reference_date=EDT
    )
    assert plan.cron_utc == "0 6 * * 1-5"


def test_non_gated_frequency_always_runs():
    plan = resolve_schedule({"frequency": "daily"}, reference_date=EDT)
    assert plan.should_run_on(date(2026, 1, 1)) is True
    assert plan.should_run_on(date(2026, 6, 30)) is True


@pytest.mark.parametrize("bad", [
    {"frequency": "hourly"},
    {"frequency": "daily", "run_time": "25:00"},
    {"frequency": "daily", "run_time": "8am"},
    {"frequency": "weekly", "day_of_week": "funday"},
    {"frequency": "monthly", "day_of_month": 31},
    {"frequency": "monthly", "day_of_month": 0},
    {"frequency": "daily", "timezone": "Mars/Phobos"},
])
def test_invalid_config_raises(bad):
    with pytest.raises(ScheduleError):
        resolve_schedule(bad, reference_date=EDT)


def test_empty_schedule_defaults_to_daily():
    plan = resolve_schedule(None, reference_date=EDT)
    assert plan.frequency == "daily"
    assert plan.cron_utc == "30 12 * * 1-5"
