"""Daily Moscow working intervals; outbound automation and approval clocks only."""
from datetime import datetime, time, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3))


def parse(value):
    if not isinstance(value, str) or len(value) != 5:
        raise ValueError("Укажите время в формате ЧЧ:ММ")
    result = time.fromisoformat(value)
    if result.second or result.microsecond:
        raise ValueError("Укажите часы и минуты")
    return result


def validate(settings):
    if settings.get("business_hours_enabled"):
        start, end = parse(settings.get("business_start")), parse(settings.get("business_end"))
        if start == end:
            raise ValueError("Начало и конец рабочего времени должны отличаться; для круглосуточной работы отключите расписание")


def moment(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def intervals(settings, start, end):
    day = start.astimezone(MOSCOW).date() - timedelta(days=1)
    last = end.astimezone(MOSCOW).date()
    opening, closing = parse(settings["business_start"]), parse(settings["business_end"])
    while day <= last:
        left = datetime.combine(day, opening, MOSCOW)
        right = datetime.combine(day, closing, MOSCOW)
        if closing < opening:
            right += timedelta(days=1)
        yield left, right
        day += timedelta(days=1)


def is_open(settings, now):
    if not settings.get("business_hours_enabled"):
        return True
    now = moment(now)
    return any(left <= now < right for left, right in intervals(settings, now, now))


def seconds_between(settings, start, end):
    start, end = moment(start), moment(end)
    if end <= start:
        return 0
    if not settings.get("business_hours_enabled"):
        return (end - start).total_seconds()
    return sum(max(0, (min(end, right) - max(start, left)).total_seconds())
               for left, right in intervals(settings, start, end))


def add_seconds(settings, start, seconds):
    start = moment(start)
    if not settings.get("business_hours_enabled"):
        return (start + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()
    # Even a one-minute shift supports the maximum 72-hour approval period.
    for left, right in intervals(settings, start, start + timedelta(days=int(seconds / 60) + 2)):
        left = max(start, left)
        if left >= right:
            continue
        available = (right - left).total_seconds()
        if seconds <= available:
            return (left + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()
        seconds -= available
    raise ValueError("Не удалось рассчитать рабочее время")
