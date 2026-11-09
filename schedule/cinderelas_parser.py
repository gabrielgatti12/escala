"""Parse the final monthly Cinderelas grids without reading or writing the DB.

Only 18:00–24:00 rows belong to this import. A slot key is
``tab|ISO-date|group|18-24|ordinal``. The ordinal is one-based within a date and
group and counts *every* matching time row, including vacant or placeholder
rows. Names and physical row numbers never participate in that identity.
"""
from collections import Counter
from datetime import date, datetime, time, timedelta
import re
import unicodedata
from zoneinfo import ZoneInfo

from openpyxl.utils import get_column_letter


SAO_PAULO = ZoneInfo('America/Sao_Paulo')
DAY_COLUMNS = (2, 5, 8, 11, 14, 17, 20)
WEEKDAYS = ('SEGUNDA', 'TERCA', 'QUARTA', 'QUINTA', 'SEXTA', 'SABADO', 'DOMINGO')
MONTHS = {
    name: number for number, name in enumerate((
        'JANEIRO', 'FEVEREIRO', 'MARCO', 'ABRIL', 'MAIO', 'JUNHO',
        'JULHO', 'AGOSTO', 'SETEMBRO', 'OUTUBRO', 'NOVEMBRO', 'DEZEMBRO',
    ), 1)
}
HOURS = re.compile(r'^\s*(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*$')


def _normalized(value):
    value = unicodedata.normalize('NFKD', str(value or ''))
    return ''.join(char for char in value.upper() if char.isascii() and char.isalnum())


def _month(title):
    match = re.fullmatch(r'([A-Z]+)(\d{4})', _normalized(title))
    if not match or match[1] not in MONTHS or not 1 <= int(match[2]) <= 9998:
        return None
    return int(match[2]), MONTHS[match[1]]


def month_key(title):
    """Return the logical YYYY-MM identity, independent of tab spelling."""
    month = _month(title)
    return f'{month[0]:04d}-{month[1]:02d}' if month else None


def _eligible_hours(value):
    match = HOURS.fullmatch(str(value or ''))
    return bool(match and int(match[1]) == 18 and int(match[2] or 0) == 0
                and int(match[3]) in (0, 24) and int(match[4] or 0) == 0)


def _value(rows, row, column):
    return rows[row - 1][column - 1] if row <= len(rows) and column <= len(rows[row - 1]) else None


def _header_matches(rows, row):
    count = 0
    for column, expected in zip(DAY_COLUMNS, WEEKDAYS):
        label = _normalized(_value(rows, row, column))
        if label in (expected, expected + 'FEIRA'):
            count += 1
    return count


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # Text dates without a full year or Excel serials require interpretation;
    # reject them instead of borrowing a year/day from adjacent columns.
    return None


def _cinderela_range(value):
    if not _eligible_hours(value):
        return None
    return {'start': time(18), 'end': time(), 'day_offset': 1, 'normalized_hours': '18-24'}


def parse_cinderelas(book):
    """Return validated monthly slots and the tabs whose prior data must remain.

    Optional ``book._uncached_formula_cells`` maps tab titles to coordinate sets
    supplied by the XLSX loader. A formula-only name cell cannot be mistaken for
    an intentional vacancy when that metadata is available.
    """
    return _parse_monthly(book, hours_parser=_cinderela_range, label='Cinderelas', rows_label='linhas 18–24')


def _parse_monthly(book, *, hours_parser, label, rows_label, strict_hours=False):
    """Shared grid validation; the public Cinderelas parser retains its filter."""
    result = {'slots': [], 'warnings': [], 'preserved_tabs': set(), 'recognized_tabs': set()}
    uncached = getattr(book, '_uncached_formula_cells', {})
    monthly_sheets = []
    for sheet in book:
        month = _month(sheet.title)
        if month is not None:
            monthly_sheets.append((sheet, month))
    month_counts = Counter(month for sheet, month in monthly_sheets)
    for sheet, month in monthly_sheets:
        tab = sheet.title
        result['recognized_tabs'].add(tab)
        if month_counts[month] > 1:
            result['preserved_tabs'].add(tab)
            result['warnings'].append(f'{tab}: mais de uma aba representa {month_key(tab)}; grades ambíguas, dados anteriores preservados.')
            continue
        errors = False
        slots = []
        if sheet.sheet_state != 'visible':
            result['preserved_tabs'].add(tab)
            result['warnings'].append(f'{tab}: aba mensal oculta; dados anteriores preservados.')
            continue
        rows = list(sheet.iter_rows(values_only=True))
        header_rows = []
        for row in range(1, len(rows) + 1):
            matches = _header_matches(rows, row)
            if matches >= 3:
                header_rows.append(row)
                if matches != 7:
                    errors = True
                    result['warnings'].append(f'{tab}: cabeçalho semanal incompleto na linha {row}.')
        if not header_rows:
            result['preserved_tabs'].add(tab)
            result['warnings'].append(f'{tab}: grade mensal ausente ou vazia; dados anteriores preservados.')
            continue
        ordinals = Counter()
        eligible_rows = 0
        tab_uncached = uncached.get(tab, set()) if isinstance(uncached, dict) else set()
        for header_row, next_header in zip(header_rows, header_rows[1:] + [len(rows) + 1]):
            days = {}
            for column in DAY_COLUMNS:
                raw_day = _value(rows, header_row + 1, column)
                days[column] = _day(raw_day)
                if raw_day is not None and days[column] is None:
                    coordinate = f'{get_column_letter(column)}{header_row + 1}'
                    result['warnings'].append(f'{tab}: data inválida em {coordinate}; nenhum dia foi inferido.')
            group = None
            for row in range(header_row + 2, next_header):
                marker = _normalized(_value(rows, row, 1))
                if marker in ('R1', 'R2', 'R3'):
                    group = marker
                for column in DAY_COLUMNS:
                    raw_hours = _value(rows, row, column)
                    hour_cell = f'{get_column_letter(column)}{row}'
                    name = _value(rows, row, column + 1)
                    if (hour_cell in tab_uncached or (isinstance(raw_hours, str) and raw_hours.startswith('='))) and name:
                        errors = True
                        result['warnings'].append(f'{tab}: horário por fórmula sem valor confirmado em {hour_cell}; aba preservada.')
                        continue
                    hours = hours_parser(raw_hours)
                    if hours is None:
                        if strict_hours and name and _normalized(name) not in ('COBUNEX', 'X'):
                            errors = True
                            result['warnings'].append(f'{tab}: horário inválido ou ausente em {hour_cell}; aba preservada.')
                        continue
                    eligible_rows += 1
                    day = days[column]
                    ordinal_key = (day, group, hours['normalized_hours']) if day else (header_row, column, group, hours['normalized_hours'])
                    ordinals[ordinal_key] += 1
                    cell = f'{get_column_letter(column + 1)}{row}'
                    if cell in tab_uncached or (isinstance(name, str) and name.startswith('=')):
                        errors = True
                        result['warnings'].append(f'{tab}: fórmula sem valor confirmado em {cell}; aba preservada.')
                        continue
                    if name is None or (isinstance(name, str) and not name.strip()):
                        continue
                    if _normalized(name) in ('COBUNEX', 'X'):
                        marker_label = 'COB UNEX' if _normalized(name) == 'COBUNEX' else 'x'
                        result['warnings'].append(f'{tab}: marcador {marker_label} em {cell} ignorado; não é residente.')
                        continue
                    if not isinstance(name, str) or name.startswith('#'):
                        errors = True
                        result['warnings'].append(f'{tab}: nome sem valor válido em {cell}; aba preservada.')
                        continue
                    if day is None or (day.year, day.month) != month:
                        errors = True
                        result['warnings'].append(f"{tab}: atividade {hours['normalized_hours'].replace('-', '–')} em {cell} depende de data inválida ou fora do mês; aba preservada.")
                        continue
                    if group is None:
                        errors = True
                        result['warnings'].append(f'{tab}: grupo R1/R2/R3 ausente em {cell}; aba preservada.')
                        continue
                    slot = {
                        'tab': tab, 'cell': cell, 'date': day,
                        'starts_at': datetime.combine(day, hours['start'], SAO_PAULO),
                        'ends_at': datetime.combine(day + timedelta(days=hours['day_offset']), hours['end'], SAO_PAULO),
                        'group': group, 'name': name.strip(),
                        'slot_key': f"{tab}|{day.isoformat()}|{group}|{hours['normalized_hours']}|{ordinals[ordinal_key]}",
                    }
                    if 'category' in hours:
                        slot.update(category=hours['category'], normalized_hours=hours['normalized_hours'])
                    slots.append(slot)
        if errors or not slots:
            result['preserved_tabs'].add(tab)
            if not errors:
                detail = f'sem {rows_label}' if not eligible_rows else f'sem residentes em {rows_label}'
                result['warnings'].append(f'{tab}: mês {detail}; dados anteriores preservados.')
        else:
            result['slots'].extend(slots)
    if not result['recognized_tabs']:
        from .sheets import SheetFormatError
        raise SheetFormatError(f'Nenhuma aba mensal reconhecida em {label}; informe mês e ano no nome das abas.')
    return result
