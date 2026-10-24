"""Schedules say Mon-Fri in normal cron numbers; the scheduler must run them Monday to Friday (APScheduler counts from Monday=0)."""
import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("apscheduler")
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

spec = importlib.util.spec_from_file_location("scheduler_main_cron", Path(__file__).resolve().parent.parent / "scheduler" / "main.py")
sched = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sched)

IST = ZoneInfo("Asia/Kolkata")


@pytest.mark.parametrize("cron, apscheduler", [("1-5", "mon-fri"), ("0", "sun"), ("5", "fri"), ("7", "sun"), ("1,3", "mon,wed"), ("*", "*"), ("*/2", "*/2"), ("mon-fri", "mon-fri")])
def test_weekday_numbers_are_translated(cron, apscheduler):
    assert sched.cron_weekdays(cron) == apscheduler


def test_a_mon_fri_job_fires_on_monday_and_not_on_saturday():
    trigger = CronTrigger(minute="45", hour="7", day_of_week=sched.cron_weekdays("1-5"), timezone=IST)
    day, fires = datetime(2026, 10, 23, 12, 0, tzinfo=IST), []          # a Friday noon
    for _ in range(6):
        day = trigger.get_next_fire_time(None, day)
        fires.append(day.strftime("%a"))
        day = day.replace(hour=12)
    assert fires == ["Mon", "Tue", "Wed", "Thu", "Fri", "Mon"]          # Sat and Sun never appear
