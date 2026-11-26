from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .constants import DEFAULT_TIMEZONE


def validate_timezone(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timezone must be 'system' or a valid IANA timezone, such as Europe/Amsterdam")
    value = value.strip()
    if value.lower() == DEFAULT_TIMEZONE:
        return DEFAULT_TIMEZONE
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            "Timezone must be 'system' or a valid IANA timezone, such as Europe/Amsterdam"
        ) from exc
    return value


def current_time(store, clock):
    name = validate_timezone(store.get("timezone", DEFAULT_TIMEZONE))
    zone = None if name == DEFAULT_TIMEZONE else ZoneInfo(name)

    # Converting from UTC applies the current system or IANA timezone, including DST.
    return clock.now(timezone.utc).astimezone(zone)
