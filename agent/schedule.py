"""Run-cadence resolution.

The review runs on a cadence you choose in ``config.yaml → schedule.frequency``:
``daily`` (default), ``weekly``, ``biweekly``, or ``monthly``. A Routine / cron fires the
review; this module turns your human-friendly config into the **UTC cron expression** to put
in that Routine, plus a run-gate for the one cadence a plain cron can't express (biweekly).

Why UTC: cron schedules (Routine or system cron) evaluate in UTC, but you configure a local
wall-clock time (e.g. 08:30 America/New_York, pre-market). We convert using the timezone's
offset **on a reference date**, so daylight-saving is accounted for. Because the US pre-market
time shifts between 12:30 UTC (EDT) and 13:30 UTC (EST), re-derive the cron when the season
changes — ``python -m agent.schedule`` prints the current value, and the daily Routine's setup
uses it.

Nothing here executes trades or touches the broker; it only computes *when* to run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FREQUENCIES: tuple[str, ...] = ("daily", "weekly", "biweekly", "monthly")

# cron day-of-week: 0 and 7 are Sunday.
_WEEKDAY_TO_CRON: dict[str, int] = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_CRON_TO_WEEKDAY = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 0: "Sun"}

DEFAULT_RUN_TIME = "08:30"
DEFAULT_TIMEZONE = "America/New_York"
# ISO-week parity biweekly runs on. Even weeks keeps it deterministic and easy to reason about.
_BIWEEKLY_EVEN_WEEKS = True


class ScheduleError(ValueError):
    """Raised when the schedule configuration is invalid."""


@dataclass(frozen=True)
class SchedulePlan:
    """The resolved cadence: what cron to use and how to describe it."""

    frequency: str
    cron_utc: str
    timezone: str
    local_time: str
    description: str
    needs_run_gate: bool  # True when cron alone can't express the cadence (biweekly)

    def should_run_on(self, day: date) -> bool:
        """Whether a firing on ``day`` should actually run.

        Cron handles daily/weekly/monthly exactly, so those always return True. Biweekly is
        expressed as a weekly cron plus this gate: it runs only on even ISO weeks (or odd,
        if configured), so a weekly-firing Routine effectively runs every other week.
        """
        if not self.needs_run_gate:
            return True
        iso_week = day.isocalendar()[1]
        return (iso_week % 2 == 0) if _BIWEEKLY_EVEN_WEEKS else (iso_week % 2 == 1)


def _parse_run_time(raw: Any) -> tuple[int, int]:
    text = str(raw or DEFAULT_RUN_TIME).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleError(f"schedule.run_time must be 'HH:MM', got {text!r}")
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ScheduleError(f"schedule.run_time must be 'HH:MM', got {text!r}") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ScheduleError(f"schedule.run_time out of range: {text!r}")
    return hh, mm


def _local_to_utc(
    hh: int, mm: int, tz_name: str, reference_date: date
) -> tuple[int, int, int]:
    """Convert a local wall-clock time to UTC on a reference date.

    Returns ``(utc_hour, utc_minute, day_delta)`` where ``day_delta`` is the calendar-day
    shift (-1, 0, or +1) the conversion causes — needed to move a weekly/monthly cron field
    when the local→UTC change crosses midnight.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        raise ScheduleError(
            f"unknown timezone {tz_name!r}; set schedule.timezone to an IANA name "
            "(e.g. 'America/New_York') or set an explicit schedule.cron override"
        ) from exc
    local_dt = datetime(
        reference_date.year, reference_date.month, reference_date.day, hh, mm, tzinfo=tz
    )
    utc_dt = local_dt.astimezone(timezone.utc)
    day_delta = (utc_dt.date() - reference_date).days
    return utc_dt.hour, utc_dt.minute, day_delta


def resolve_schedule(
    schedule: dict[str, Any] | None, reference_date: date | None = None
) -> SchedulePlan:
    """Turn a ``schedule`` config block into a :class:`SchedulePlan`.

    ``reference_date`` picks the daylight-saving offset (defaults to today); pass it in tests
    for determinism. An explicit ``schedule.cron`` wins over the derived value.
    """
    schedule = dict(schedule or {})
    reference_date = reference_date or date.today()

    frequency = str(schedule.get("frequency", "daily")).strip().lower()
    if frequency not in FREQUENCIES:
        raise ScheduleError(
            f"schedule.frequency must be one of {', '.join(FREQUENCIES)}, got {frequency!r}"
        )

    tz_name = str(schedule.get("timezone", DEFAULT_TIMEZONE)).strip() or DEFAULT_TIMEZONE
    local_time_raw = schedule.get("run_time", DEFAULT_RUN_TIME)
    hh, mm = _parse_run_time(local_time_raw)
    local_time = f"{hh:02d}:{mm:02d}"

    # Explicit override: trust it verbatim (still report the intended cadence).
    override = str(schedule.get("cron", "") or "").strip()
    if override:
        return SchedulePlan(
            frequency=frequency,
            cron_utc=override,
            timezone=tz_name,
            local_time=local_time,
            description=f"{frequency} — custom cron override '{override}' (UTC)",
            needs_run_gate=frequency == "biweekly",
        )

    uh, um, day_delta = _local_to_utc(hh, mm, tz_name, reference_date)

    if frequency == "daily":
        # Trading review: weekdays only (markets are closed on weekends).
        cron = f"{um} {uh} * * 1-5"
        desc = f"every weekday at {local_time} {tz_name} ({uh:02d}:{um:02d} UTC)"
        gate = False
    elif frequency in ("weekly", "biweekly"):
        dow_name = str(schedule.get("day_of_week", "mon")).strip().lower()[:3]
        if dow_name not in _WEEKDAY_TO_CRON:
            raise ScheduleError(
                f"schedule.day_of_week must be one of mon..sun, got "
                f"{schedule.get('day_of_week')!r}"
            )
        dow = (_WEEKDAY_TO_CRON[dow_name] + day_delta) % 7
        cron = f"{um} {uh} * * {dow}"
        cadence = "every week" if frequency == "weekly" else "every other week"
        desc = (
            f"{cadence} on {_CRON_TO_WEEKDAY[dow % 7]} at {local_time} {tz_name} "
            f"({uh:02d}:{um:02d} UTC)"
        )
        if frequency == "biweekly":
            desc += " — gated to even ISO weeks"
        gate = frequency == "biweekly"
    else:  # monthly
        dom = int(schedule.get("day_of_month", 1))
        if not (1 <= dom <= 28):
            raise ScheduleError(
                f"schedule.day_of_month must be 1-28 (to exist in every month), got {dom}"
            )
        dom = dom + day_delta  # 0 for morning US times; only shifts if local->UTC crosses midnight
        if not (1 <= dom <= 28):
            raise ScheduleError(
                "monthly run_time converts to a UTC day-of-month outside 1-28 for this "
                "timezone; choose an earlier/later run_time or set an explicit schedule.cron"
            )
        cron = f"{um} {uh} {dom} * *"
        desc = f"monthly on day {dom} at {local_time} {tz_name} ({uh:02d}:{um:02d} UTC)"
        gate = False

    return SchedulePlan(
        frequency=frequency,
        cron_utc=cron,
        timezone=tz_name,
        local_time=local_time,
        description=desc,
        needs_run_gate=gate,
    )


def main(argv: list[str] | None = None) -> int:
    """Print the cron to configure for the current ``config.yaml`` cadence."""
    import argparse

    from agent.settings import load_settings

    parser = argparse.ArgumentParser(
        description="Show the UTC cron for the configured review cadence."
    )
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    plan = resolve_schedule(settings.schedule)
    print(f"Cadence     : {plan.frequency}")
    print(f"Runs        : {plan.description}")
    print(f"Cron (UTC)  : {plan.cron_utc}")
    if plan.needs_run_gate:
        print("Note        : biweekly uses a weekly cron + an in-run gate (even ISO weeks).")
    print(
        "\nPoint the daily Routine at this cron. US pre-market shifts with DST, so re-run "
        "this after a clock change (or set an explicit schedule.cron)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
