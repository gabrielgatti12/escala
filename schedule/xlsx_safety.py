"""Bound XLSX expansion and worksheet extents before openpyxl iterates cells."""
from dataclasses import dataclass
from io import BytesIO
import re
from xml.etree.ElementTree import ParseError, XMLParser
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED, ZIP_STORED


MAX_EXPORT_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_SHEET_CELLS = 2_000_000
MAX_WORKBOOK_CELLS = 5_000_000
CELL = re.compile(r'([A-Za-z]{1,3})([1-9][0-9]{0,6})')


class UnsafeWorkbookError(ValueError):
    pass


@dataclass(frozen=True)
class _PartBudget:
    rows: int = 0
    columns: int = 0
    cells: int = 0
    row_elements: int = 0
    empty_formula_strings: frozenset = frozenset()

    @property
    def cost(self):
        # Sparse geometry and repeated physical records impose different costs.
        return max(self.rows * self.columns, self.cells, self.row_elements)


class _XmlBudget:
    """Count geometry without creating elements or filling sparse row gaps."""
    def __init__(self):
        self.worksheet = None
        self.rows = 0
        self.columns = 0
        self.cells = 0
        self.row_elements = 0
        self.row_counter = 0
        self.column_counter = 0
        self.empty_formula_strings = set()
        self.cache_order_safe = True
        self.cell = None
        self.in_value = False

    def _extent(self, row, column):
        self.rows = max(self.rows, row)
        self.columns = max(self.columns, column)
        if self.rows * self.columns > MAX_SHEET_CELLS:
            raise UnsafeWorkbookError('Uma aba excede o limite de 2 milhões de células; dados anteriores preservados.')

    def _coordinate(self, value):
        match = CELL.fullmatch(value)
        if not match:
            raise UnsafeWorkbookError('A planilha contém coordenadas de células inválidas.')
        column = 0
        for char in match[1].upper():
            column = column * 26 + ord(char) - ord('A') + 1
        row = int(match[2])
        if row > 1_048_576 or column > 16_384:
            raise UnsafeWorkbookError('A planilha contém coordenadas fora dos limites do XLSX.')
        self._extent(row, column)
        return row, column

    def start(self, tag, attributes):
        local = tag.rsplit('}', 1)[-1]
        if self.worksheet is None:
            self.worksheet = local == 'worksheet'
        if not self.worksheet:
            return
        if local == 'dimension' and attributes.get('ref'):
            for coordinate in attributes['ref'].split(':'):
                self._coordinate(coordinate)
        elif local == 'row':
            self.row_elements += 1
            if self.row_elements > MAX_SHEET_CELLS:
                raise UnsafeWorkbookError('Uma aba excede o limite de 2 milhões de registros de linhas; dados anteriores preservados.')
            previous_row = self.row_counter
            value = attributes.get('r')
            if value is not None:
                if not re.fullmatch(r'[1-9][0-9]{0,6}', value):
                    raise UnsafeWorkbookError('A planilha contém um índice de linha inválido.')
                self.row_counter = int(value)
            else:
                self.row_counter += 1
            if self.row_counter > 1_048_576:
                raise UnsafeWorkbookError('A planilha contém um índice de linha inválido.')
            if self.row_counter <= previous_row:
                self.cache_order_safe = False
                self.empty_formula_strings.clear()
            self.column_counter = 0
            self._extent(self.row_counter, 0)
        elif local == 'c':
            self.cells += 1
            if self.cells > MAX_SHEET_CELLS:
                raise UnsafeWorkbookError('Uma aba excede o limite de 2 milhões de registros de células; dados anteriores preservados.')
            previous_column = self.column_counter
            if attributes.get('r'):
                cell_row, self.column_counter = self._coordinate(attributes['r'])
            else:
                cell_row = self.row_counter
                self.column_counter += 1
                if not 1 <= self.row_counter <= 1_048_576 or self.column_counter > 16_384:
                    raise UnsafeWorkbookError('A planilha contém coordenadas fora dos limites do XLSX.')
                self._extent(self.row_counter, self.column_counter)
            if cell_row != self.row_counter or self.column_counter <= previous_column:
                # Do not confuse duplicate/out-of-order records with the cell
                # that openpyxl ultimately exposes to the schedule parser.
                self.cache_order_safe = False
                self.empty_formula_strings.clear()
            self.cell = {
                'coordinate': (cell_row, self.column_counter),
                'string': attributes.get('t') == 'str', 'formula': False,
                'value_seen': False, 'value_has_text': False,
            }
            self.in_value = False
        elif self.cell is not None and local == 'f':
            self.cell['formula'] = True
        elif self.cell is not None and local == 'v':
            self.cell['value_seen'] = True
            self.in_value = True

    def end(self, tag):
        local = tag.rsplit('}', 1)[-1]
        if local == 'v':
            self.in_value = False
        elif local == 'c' and self.cell is not None:
            if (self.cache_order_safe and self.cell['string'] and self.cell['formula']
                    and self.cell['value_seen'] and not self.cell['value_has_text']):
                self.empty_formula_strings.add(self.cell['coordinate'])
            self.cell = None
            self.in_value = False

    def data(self, value):
        if self.cell is not None and self.in_value and value:
            self.cell['value_has_text'] = True

    def doctype(self, name, public_id, system_id):
        raise UnsafeWorkbookError('Declarações DTD não são permitidas em planilhas XLSX.')

    def close(self):
        return _PartBudget(self.rows, self.columns, self.cells, self.row_elements,
                           frozenset(self.empty_formula_strings))


def _inspect_part(archive, name):
    parser = XMLParser(target=_XmlBudget())
    with archive.open(name) as stream:
        while chunk := stream.read(65536):
            parser.feed(chunk)
    return parser.close()


def inspect_xlsx(data):
    """Return per-part extents after validating compressed and expanded budgets.

    Limits bound work and memory; they are not an execution-time guarantee.
    """
    if len(data) > MAX_EXPORT_BYTES:
        raise UnsafeWorkbookError('Planilha excede o limite de 20 MB.')
    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise UnsafeWorkbookError('O XLSX excede o limite de 4096 arquivos internos.')
            if len({member.filename for member in members}) != len(members):
                raise UnsafeWorkbookError('O XLSX contém arquivos internos duplicados.')
            if sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
                raise UnsafeWorkbookError('O XLSX excede o limite de 128 MB após descompactação.')
            if any(member.flag_bits & 1 or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED) for member in members):
                raise UnsafeWorkbookError('O XLSX usa criptografia ou compactação não suportada.')
            extents = {}
            for member in members:
                if member.filename.lower().endswith(('.xml', '.rels')):
                    extents[member.filename] = _inspect_part(archive, member.filename)
            if sum(part.cost for part in extents.values()) > MAX_WORKBOOK_CELLS:
                raise UnsafeWorkbookError('O XLSX excede o limite total de 5 milhões de células.')
            return extents
    except (BadZipFile, ParseError, OSError, RuntimeError, KeyError) as error:
        raise UnsafeWorkbookError('Arquivo XLSX inválido ou ilegível; dados anteriores preservados.') from error


def validate_loaded_workbook(book, data, extents):
    """Also cover unusual worksheet part names and repeated part references."""
    total = 0
    try:
        with ZipFile(BytesIO(data)) as archive:
            for sheet in book:
                part = getattr(sheet, '_worksheet_path', None)
                if part and part not in extents:
                    extents[part] = _inspect_part(archive, part)
                budget = extents.get(part, _PartBudget())
                rows = max(budget.rows, sheet.max_row or 0)
                columns = max(budget.columns, sheet.max_column or 0)
                cost = max(rows * columns, budget.cost)
                if cost > MAX_SHEET_CELLS:
                    raise UnsafeWorkbookError('Uma aba excede o limite de 2 milhões de células; dados anteriores preservados.')
                total += cost
                if total > MAX_WORKBOOK_CELLS:
                    raise UnsafeWorkbookError('O XLSX excede o limite total de 5 milhões de células.')
    except (BadZipFile, ParseError, OSError, RuntimeError, KeyError) as error:
        raise UnsafeWorkbookError('Arquivo XLSX inválido ou ilegível; dados anteriores preservados.') from error
