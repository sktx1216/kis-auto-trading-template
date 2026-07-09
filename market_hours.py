from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config


try:
    MARKET_TZ = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    MARKET_TZ = None

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def is_us_market_open(now=None):
    if not config.MARKET_HOURS_GUARD:
        return True

    current = _eastern_now(now)
    if not is_us_market_day(current):
        return False

    return MARKET_OPEN <= current.time() < MARKET_CLOSE


def is_us_market_day(now=None):
    current = _eastern_now(now) if now is None or hasattr(now, "hour") else _coerce_date(now)
    current_date = current.date() if hasattr(current, "date") else current
    if current_date.weekday() >= 5:
        return False
    return current_date not in us_market_holidays(current_date.year)


def market_status_note(now=None):
    current = _eastern_now(now)
    return {
        "is_open": is_us_market_open(current),
        "is_market_day": is_us_market_day(current),
        "market_time": current.isoformat(),
        "note": "US regular market hours only. Major NYSE holidays are skipped; half-days are treated as market days.",
    }


def market_date_key(now=None):
    return _eastern_now(now).strftime("%Y-%m-%d")


def trading_days_between(start_date, end_date=None):
    start = _parse_date(start_date)
    end = _parse_date(end_date or market_date_key())
    if end <= start:
        return 0

    days = 0
    current = start + timedelta(days=1)
    while current <= end:
        if is_us_market_day(current):
            days += 1
        current += timedelta(days=1)
    return days


def us_market_holidays(year):
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _good_friday(year),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    return holidays


def _eastern_now(now=None):
    if MARKET_TZ is not None:
        current = now or datetime.now(MARKET_TZ)
        return current.astimezone(MARKET_TZ)

    utc_now = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    offset = -4 if _is_us_dst(utc_now.date()) else -5
    return utc_now.astimezone(timezone(timedelta(hours=offset)))


def _is_us_dst(day):
    year = day.year
    dst_start = _nth_weekday(year, 3, 6, 2)
    dst_end = _nth_weekday(year, 11, 6, 1)
    return dst_start <= day < dst_end


def _nth_weekday(year, month, weekday, n):
    day = date(year, month, 1)
    days_until_weekday = (weekday - day.weekday()) % 7
    return day + timedelta(days=days_until_weekday + (n - 1) * 7)


def _last_weekday(year, month, weekday):
    if month == 12:
        day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    days_since_weekday = (day.weekday() - weekday) % 7
    return day - timedelta(days=days_since_weekday)


def _observed(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _good_friday(year):
    return _easter_sunday(year) - timedelta(days=2)


def _easter_sunday(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _parse_date(value):
    if hasattr(value, "date"):
        return value.date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _coerce_date(value):
    if isinstance(value, date):
        return value
    return _parse_date(value)
