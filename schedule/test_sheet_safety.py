from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from openpyxl import Workbook

from .management.commands.import_schedule import Command
from .models import IncompleteActivity, Notification, ResidentAccess, Shift, SyncState
from .sheets import SheetFormatError, fetch_workbook, load_export, sync_workbook


def workbook_bytes(book):
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def rewrite_worksheet(data, transform):
    stream = BytesIO()
    with ZipFile(BytesIO(data)) as original, ZipFile(stream, 'w', ZIP_DEFLATED) as result:
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename.startswith('xl/worksheets/'):
                content = transform(content)
            result.writestr(item.filename, content)
    return stream.getvalue()


class XlsxBudgetTests(SimpleTestCase):
    def small_book(self, sheets=1):
        book = Workbook()
        book.active['A1'] = 'Safe fixture'
        for index in range(1, sheets):
            book.create_sheet(f'Tab {index}')['A1'] = 'Safe fixture'
        return workbook_bytes(book)

    def test_declared_sparse_huge_grid_is_rejected_before_openpyxl(self):
        data = rewrite_worksheet(self.small_book(), lambda xml: re.sub(
            rb'<dimension ref="[^"]+"', b'<dimension ref="A1:XFD1048576"', xml,
        ))
        self.assertLess(len(data), 20_000)
        with patch('schedule.sheets.load_workbook') as loader:
            with self.assertRaisesMessage(SheetFormatError, '2 milhões'):
                load_export(data)
            loader.assert_not_called()

    def test_missing_dimension_cannot_bypass_actual_coordinate_budget(self):
        def transform(xml):
            xml = re.sub(rb'<dimension[^>]*/>', b'', xml)
            xml = xml.replace(b'<row r="1"', b'<row r="1048576"')
            return xml.replace(b'r="A1"', b'r="XFD1048576"')
        data = rewrite_worksheet(self.small_book(), transform)
        with self.assertRaisesMessage(SheetFormatError, '2 milhões'):
            load_export(data)

    def test_total_workbook_budget_applies_across_individually_valid_sheets(self):
        data = rewrite_worksheet(self.small_book(sheets=3), lambda xml: re.sub(
            rb'<dimension ref="[^"]+"', b'<dimension ref="A1:AL50000"', xml,
        ))
        with self.assertRaisesMessage(SheetFormatError, '5 milhões'):
            load_export(data)

    def test_expanded_zip_budget_is_checked_before_parsing_xml(self):
        with patch('schedule.xlsx_safety.MAX_EXPANDED_BYTES', 100):
            with patch('schedule.sheets.load_workbook') as loader:
                with self.assertRaisesMessage(SheetFormatError, '128 MB'):
                    load_export(self.small_book())
                loader.assert_not_called()

    def test_zip_member_count_is_bounded(self):
        stream = BytesIO()
        with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
            for number in range(4097):
                archive.writestr(f'fixture-{number}.bin', b'')
        with self.assertRaisesMessage(SheetFormatError, '4096'):
            load_export(stream.getvalue())

    def test_xml_dtd_is_rejected_without_expanding_entities(self):
        data = rewrite_worksheet(self.small_book(), lambda xml:
            b'<!DOCTYPE worksheet [<!ENTITY fixture "safe">]>' + xml)
        with self.assertRaisesMessage(SheetFormatError, 'DTD'):
            load_export(data)

    def raw_sheet(self, contents, sheets=1):
        xml = (b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
               b'<sheetData>' + contents + b'</sheetData></worksheet>')
        return rewrite_worksheet(self.small_book(sheets=sheets), lambda _: xml)

    def assert_rejected_before_openpyxl(self, data, message='2 milhões'):
        with patch('schedule.sheets.load_workbook') as loader:
            with self.assertRaisesMessage(SheetFormatError, message):
                load_export(data)
            loader.assert_not_called()

    def test_implicit_row_and_cell_coordinates_cannot_bypass_budget(self):
        data = self.raw_sheet(b'<row>' + b'<c><v>1</v></c>' * 12 + b'</row>')
        with patch('schedule.xlsx_safety.MAX_SHEET_CELLS', 10):
            self.assert_rejected_before_openpyxl(data)

    def test_implicit_sparse_row_geometry_is_counted(self):
        # Only six rows and two actual cells, but a twelve-cell inferred grid.
        data = self.raw_sheet(b'<row/>' * 5 + b'<row><c><v>1</v></c><c><v>2</v></c></row>')
        with patch('schedule.xlsx_safety.MAX_SHEET_CELLS', 10):
            self.assert_rejected_before_openpyxl(data)

    def test_cell_coordinates_advance_following_implicit_columns(self):
        data = self.raw_sheet(b'<row r="1"><c r="J1"><v>1</v></c><c><v>2</v></c></row>')
        with patch('schedule.xlsx_safety.MAX_SHEET_CELLS', 10):
            self.assert_rejected_before_openpyxl(data)

    def test_repeated_explicit_cells_are_bounded_even_with_small_geometry(self):
        data = self.raw_sheet(b'<row r="1">' + b'<c r="A1"><v>1</v></c>' * 12 + b'</row>')
        with patch('schedule.xlsx_safety.MAX_SHEET_CELLS', 10):
            self.assert_rejected_before_openpyxl(data)

    def test_repeated_explicit_rows_are_bounded_even_with_no_cells(self):
        data = self.raw_sheet(b'<row r="1"/>' * 12)
        with patch('schedule.xlsx_safety.MAX_SHEET_CELLS', 10):
            self.assert_rejected_before_openpyxl(data)

    def test_total_budget_includes_physical_cells_across_sheets(self):
        data = self.raw_sheet(b'<row r="1">' + b'<c r="A1"><v>1</v></c>' * 6 + b'</row>', sheets=2)
        with patch('schedule.xlsx_safety.MAX_WORKBOOK_CELLS', 10):
            self.assert_rejected_before_openpyxl(data, '5 milhões')

    def test_total_budget_includes_physical_rows_across_sheets(self):
        data = self.raw_sheet(b'<row r="1"/>' * 6, sheets=2)
        with patch('schedule.xlsx_safety.MAX_WORKBOOK_CELLS', 10):
            self.assert_rejected_before_openpyxl(data, '5 milhões')

    def test_small_implicit_grid_remains_readable(self):
        data = self.raw_sheet(b'<row><c><v>1</v></c><c><v>2</v></c></row><row><c><v>3</v></c></row>')
        book = load_export(data)
        try:
            self.assertEqual(list(book.active.values), [(1, 2), (3,)])
        finally:
            book.close()


class WeeklyIncompleteInputTests(TestCase):
    def setUp(self):
        self.source = 'weekly-input-security'
        self.book = Workbook()
        sheet = self.book.active
        sheet.title = 'Espelho Semanal'
        sheet.append([None, datetime(2035, 1, 1), None, None, None, None, None, None, datetime(2035, 1, 2)])
        sheet.append([None, 'Segunda', None, None, None, None, None, None, 'Terça'])
        sheet.append(['INSTITUTO', 'Ambulatório', None, None, None, None, None, None, 'Ambulatório'])
        sheet.append([None, 'R1', '8-12', None, None, 'Pessoa Um', None, None, 'R1', '8-12', None, None, 'Pessoa Dois'])
        sync_workbook(self.book, self.source)

    def assert_unchanged_after_rejection(self, book):
        before = list(Shift.objects.order_by('pk').values())
        last_success = SyncState.objects.get(source=self.source).last_success
        with self.assertRaises(SheetFormatError):
            sync_workbook(book, self.source)
        self.assertEqual(list(Shift.objects.order_by('pk').values()), before)
        self.assertEqual(SyncState.objects.get(source=self.source).last_success, last_success)
        self.assertFalse(Notification.objects.exists())

    def test_uncached_name_hour_and_date_formulas_preserve_existing_shifts(self):
        for cell in ['F4', 'C4', 'B1']:
            with self.subTest(cell=cell):
                sheet = self.book.active
                previous = sheet[cell].value
                sheet[cell] = '="fixture"'
                book = load_export(workbook_bytes(self.book))
                try:
                    self.assertIn(cell, book._uncached_formula_cells['Espelho Semanal'])
                    self.assert_unchanged_after_rejection(book)
                finally:
                    book.close()
                    sheet[cell] = previous

    def test_missing_one_day_header_with_residents_preserves_the_entire_source(self):
        self.book.active['I1'] = None
        self.assert_unchanged_after_rejection(self.book)

    def test_intentionally_empty_day_may_be_removed_without_inventing_a_date(self):
        self.book.active['I1'] = None
        self.book.active['M4'] = None
        sync_workbook(self.book, self.source)
        self.assertEqual(Shift.objects.filter(source=self.source, active=True).count(), 1)

    def test_formula_inspection_reuses_the_same_download(self):
        self.book.active['F4'] = '="fixture"'
        with patch('schedule.sheets.urlopen', return_value=BytesIO(workbook_bytes(self.book))) as download:
            book = fetch_workbook('a' * 30)
            try:
                self.assertIn('F4', book._uncached_formula_cells['Espelho Semanal'])
            finally:
                book.close()
        download.assert_called_once()

    def cached_empty_cell(self, coordinate, *, value_node=True, duplicate=False):
        cache = b'<v/>' if value_node else b''
        cell = (b'<c r="' + coordinate.encode() + b'" t="str">'
                b'<f>Dados!AU74</f>' + cache + b'</c>')
        if duplicate:
            cell += (b'<c r="' + coordinate.encode() + b'" t="str">'
                     b'<f>Dados!AU74</f></c>')
        return load_export(rewrite_worksheet(workbook_bytes(self.book), lambda xml: re.sub(
            rb'<c r="' + coordinate.encode() + rb'"[^>]*>.*?</c>', cell, xml,
        )))

    def test_google_explicit_empty_string_cache_is_not_an_uncalculated_formula(self):
        book = self.cached_empty_cell('C4')
        try:
            self.assertIsNone(book.active['C4'].value)
            self.assertEqual(book.active['C4'].data_type, 'str')
            self.assertNotIn('C4', book._uncached_formula_cells.get('Espelho Semanal', set()))
            sync_workbook(book, self.source)
            self.assertEqual(Shift.objects.filter(source=self.source, active=True).count(), 1)
            self.assertEqual(IncompleteActivity.objects.filter(source=self.source).count(), 1)
        finally:
            book.close()

    def test_string_formula_without_a_value_node_still_preserves_previous_source(self):
        book = self.cached_empty_cell('C4', value_node=False)
        try:
            self.assertIn('C4', book._uncached_formula_cells['Espelho Semanal'])
            self.assert_unchanged_after_rejection(book)
        finally:
            book.close()

    def test_explicit_empty_name_cache_can_remove_an_assignment_without_inventing_names(self):
        book = self.cached_empty_cell('F4')
        try:
            self.assertNotIn('F4', book._uncached_formula_cells.get('Espelho Semanal', set()))
            sync_workbook(book, self.source)
            self.assertEqual(Shift.objects.filter(source=self.source, active=True).count(), 1)
            self.assertFalse(IncompleteActivity.objects.filter(source=self.source).exists())
        finally:
            book.close()

    def test_duplicate_cell_records_cannot_prove_an_empty_formula_cache(self):
        book = self.cached_empty_cell('C4', duplicate=True)
        try:
            self.assertIn('C4', book._uncached_formula_cells['Espelho Semanal'])
            self.assert_unchanged_after_rejection(book)
        finally:
            book.close()


class LocalImportIdentityTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.resident = users.objects.create_user('import-source-person', first_name='Pessoa Teste')
        self.account = users.objects.create_user('person@example.test')
        ResidentAccess.objects.create(account=self.account, resident=self.resident)
        self.start = timezone.make_aware(datetime(2035, 1, 1, 8))
        self.end = self.start + timedelta(hours=4)

    def import_csv(self, external_id):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.csv'
            path.write_text('id,residente,inicio,fim,setor\n'
                            f'{external_id},{self.account.username},{self.start.isoformat()},{self.end.isoformat()},Ambulatório\n')
            return Command().import_file(path)

    def test_login_alias_is_resolved_to_the_existing_resident_identity(self):
        self.import_csv('local-import-safe')
        self.assertEqual(Shift.objects.get().resident_id, self.resident.pk)
        self.assertEqual(ResidentAccess.objects.get().account_id, self.account.pk)

    def test_synchronized_source_id_cannot_be_overwritten_by_a_local_file(self):
        shift = Shift.objects.create(external_id='synced-slot', resident=self.resident,
                                     source='google-source', starts_at=self.start, ends_at=self.end, sector='Original')
        with self.assertRaisesMessage(ValueError, 'fonte sincronizada'):
            self.import_csv(shift.external_id)
        shift.refresh_from_db()
        self.assertEqual(shift.resident_id, self.resident.pk)
        self.assertEqual(shift.sector, 'Original')

    def test_inactive_resident_behind_active_login_cannot_receive_an_import(self):
        self.resident.is_active = False
        self.resident.save(update_fields=['is_active'])
        with self.assertRaisesMessage(ValueError, 'desativado'):
            self.import_csv('inactive-resident-slot')
        self.assertFalse(Shift.objects.exists())

    def test_local_xlsx_uses_the_same_grid_budget(self):
        book = Workbook()
        book.active.append(['id', 'residente', 'inicio', 'fim', 'setor'])
        data = rewrite_worksheet(workbook_bytes(book), lambda xml: re.sub(
            rb'<dimension ref="[^"]+"', b'<dimension ref="A1:XFD1048576"', xml,
        ))
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.xlsx'
            path.write_bytes(data)
            with self.assertRaisesMessage(SheetFormatError, '2 milhões'):
                Command().import_file(path)
        self.assertFalse(Shift.objects.exists())
