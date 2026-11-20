"""Times as the API writes them: in the site's zone (settings.TIME_ZONE), to the second, 2026-11-13T10:03:04+08:00.
Answers made by hand use iso(); DRF's serializer fields write theirs the same way (REST_FRAMEWORK["DATETIME_FORMAT"]).
They were three ways: UTC with microseconds, the site's zone with microseconds, and this (TEXT-01)."""

import datetime

from django.utils import timezone


def iso(moment) -> str | None:
    """In the site's zone, to the second; None when that falls outside the years 1 to 9999 (STOR-02). In
    UTC when the zone's offset then has seconds (Shanghai's +08:05:43 before 1901): browsers don't read
    those (STOR-03). A naive time (a storage's) is taken as UTC."""
    if moment is None:
        return None
    if timezone.is_naive(moment):
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    try:
        local = timezone.localtime(moment)
    except OverflowError:
        return None
    if local.utcoffset() % datetime.timedelta(minutes=1):
        local = moment.astimezone(datetime.timezone.utc)
    return local.isoformat(timespec="seconds")
