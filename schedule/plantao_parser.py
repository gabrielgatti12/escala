"""Read every explicit duty interval from the final monthly R1/R2/R3 grids.

The Cinderelas-only reader remains available for existing integrations. This
reader shares its month/date/formula validation while giving each normalized
time interval an independent ordinal within a date and resident group.
"""
from datetime import time

from .cinderelas_parser import HOURS, _parse_monthly


def parse_plantao_hours(value):
    """Return validated local clock values, without guessing absent hours."""
    match = HOURS.fullmatch(str(value or ''))
    if match is None:
        return None
    start_hour, start_minute, end_hour, end_minute = (
        int(match[1]), int(match[2] or 0), int(match[3]), int(match[4] or 0),
    )
    if start_hour > 23 or start_minute > 59 or end_hour > 24 or end_minute > 59:
        return None
    if end_hour == 24 and end_minute != 0:
        return None
    # End-of-day 00:00 and 24:00 describe the same terminal midnight. Keep the
    # old 18-24 identity even when Sheets displays 18:00–00:00.
    canonical_end_hour = 24 if end_hour == 0 and end_minute == 0 else end_hour
    start_minutes = start_hour * 60 + start_minute
    end_minutes = canonical_end_hour * 60 + end_minute
    overnight = end_minutes <= start_minutes
    day_offset = int(canonical_end_hour == 24 or overnight)

    def label(hour, minute):
        return f'{hour}:{minute:02d}' if minute else str(hour)

    normalized = f'{label(start_hour, start_minute)}-{label(canonical_end_hour, end_minute)}'
    category = 'cinderela' if normalized == '18-24' else 'night' if overnight else 'weekend'
    return {
        'start': time(start_hour, start_minute), 'end': time(end_hour % 24, end_minute),
        'day_offset': day_offset, 'normalized_hours': normalized, 'category': category,
    }


def parse_plantoes(book):
    """Return slots, warnings, recognized_tabs and safely preserved_tabs."""
    return _parse_monthly(
        book, hours_parser=parse_plantao_hours, label='Plantões',
        rows_label='linhas com horários explícitos válidos', strict_hours=True,
    )
