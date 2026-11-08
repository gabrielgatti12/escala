from datetime import date, datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase
from openpyxl import Workbook

from .cinderelas_parser import DAY_COLUMNS, WEEKDAYS, month_key, parse_cinderelas
from .sheets import SheetFormatError


class CinderelasParserTests(SimpleTestCase):
    def workbook(self, title='NOVEMBRO2026'):
        book = Workbook()
        book.active.title = title
        self.block(book.active, 2, date(2026, 11, 2))
        return book

    def block(self, sheet, header_row, monday):
        for column, weekday in zip(DAY_COLUMNS, WEEKDAYS):
            sheet.cell(header_row, column, weekday)
            sheet.cell(header_row + 1, column, datetime.combine(monday + timedelta(days=DAY_COLUMNS.index(column)), datetime.min.time()))

    def assignment(self, sheet, row=4, column=2, name='Pessoa Teste', group='R1', hours='18-24'):
        if group:
            sheet.cell(row, 1, group)
        sheet.cell(row, column, hours)
        if name is not None:
            sheet.cell(row, column + 1, name)

    def test_imports_only_18_to_midnight_for_each_group(self):
        book = self.workbook()
        sheet = book.active
        for row, hours in enumerate(['18-8', '8-8', '8-20', '9-21', '12-24', '18-24'], 4):
            self.assignment(sheet, row=row, hours=hours)
        self.assignment(sheet, row=10, group='R2', name='Segunda Pessoa')
        self.assignment(sheet, row=11, group='R3', name='Terceira Pessoa')
        result = parse_cinderelas(book)
        self.assertEqual(len(result['slots']), 3)
        self.assertEqual([slot['group'] for slot in result['slots']], ['R1', 'R2', 'R3'])
        self.assertEqual(result['preserved_tabs'], set())
        self.assertEqual(result['recognized_tabs'], {'NOVEMBRO2026'})

    def test_equivalent_time_formats_have_local_six_hour_duration(self):
        book = self.workbook()
        self.assignment(book.active, hours='18:00 – 00:00', name='  Pessoa Teste  ')
        slot = parse_cinderelas(book)['slots'][0]
        self.assertEqual(slot['name'], 'Pessoa Teste')
        self.assertEqual(slot['cell'], 'C4')
        self.assertEqual(slot['date'], date(2026, 11, 2))
        self.assertEqual(slot['starts_at'].hour, 18)
        self.assertEqual(slot['ends_at'].hour, 0)
        self.assertEqual(slot['ends_at'].date(), date(2026, 11, 3))
        self.assertEqual(slot['ends_at'] - slot['starts_at'], timedelta(hours=6))
        self.assertEqual(str(slot['starts_at'].tzinfo), 'America/Sao_Paulo')

    def test_slot_ordinal_counts_vacancies_and_nonresident_markers(self):
        book = self.workbook()
        self.assignment(book.active, row=4, name=None)
        self.assignment(book.active, row=5, name='COB UNEX', group=None)
        self.assignment(book.active, row=6, name='x', group=None)
        self.assignment(book.active, row=7, name='Pessoa Teste', group=None)
        result = parse_cinderelas(book)
        self.assertEqual(result['slots'][0]['slot_key'], 'NOVEMBRO2026|2026-11-02|R1|18-24|4')
        self.assertEqual(len(result['slots']), 1)
        self.assertEqual(len(result['warnings']), 2)
        self.assertTrue(any('COB UNEX' in warning for warning in result['warnings']))
        self.assertTrue(any('marcador x' in warning for warning in result['warnings']))

    def test_owner_and_physical_row_changes_keep_the_same_slot_key(self):
        book = self.workbook()
        self.assignment(book.active)
        first = parse_cinderelas(book)['slots'][0]
        book.active.cell(4, 3).value = 'Outra Pessoa'
        book.active.insert_rows(4, 3)
        second = parse_cinderelas(book)['slots'][0]
        self.assertEqual(first['slot_key'], second['slot_key'])
        self.assertNotEqual(first['cell'], second['cell'])
        self.assertNotEqual(first['name'], second['name'])

    def test_horizontal_name_merge_is_one_person_not_an_alternative_pair(self):
        book = self.workbook()
        self.assignment(book.active)
        book.active.merge_cells('C4:D4')
        slots = parse_cinderelas(book)['slots']
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]['name'], 'Pessoa Teste')

    def test_vertical_group_merge_carries_group_to_each_row(self):
        book = self.workbook()
        self.assignment(book.active, row=4, name='Pessoa Um')
        self.assignment(book.active, row=5, name='Pessoa Dois', group=None)
        book.active.merge_cells('A4:A5')
        slots = parse_cinderelas(book)['slots']
        self.assertEqual([slot['group'] for slot in slots], ['R1', 'R1'])
        self.assertEqual(len({slot['slot_key'] for slot in slots}), 2)

    def test_blocks_follow_headers_at_variable_intervals(self):
        book = Workbook()
        sheet = book.active
        sheet.title = 'FEVEREIRO2027'
        for index, header in enumerate([2, 24, 47, 67]):
            self.block(sheet, header, date(2027, 2, 1) + timedelta(days=index * 7))
            self.assignment(sheet, row=header + 2)
        slots = parse_cinderelas(book)['slots']
        self.assertEqual([slot['date'] for slot in slots], [date(2027, 2, day) for day in [1, 8, 15, 22]])

    def test_month_titles_accept_accents_spaces_and_separators_with_explicit_year(self):
        book = self.workbook()
        self.assignment(book.active)
        self.block(book.active, 2, date(2026, 3, 2))
        # Google tab titles may contain '/', which Excel's local title setter
        # rejects; a sheet wrapper exercises the parser with that metadata.
        sheet = SimpleNamespace(title=' março / 2026 ', sheet_state='visible', iter_rows=book.active.iter_rows)
        result = parse_cinderelas([sheet])
        self.assertEqual(result['recognized_tabs'], {' março / 2026 '})
        self.assertEqual(result['slots'][0]['date'], date(2026, 3, 2))

    def test_month_key_is_stable_across_title_spellings(self):
        self.assertEqual(month_key('MARÇO2026'), '2026-03')
        self.assertEqual(month_key(' março / 2026 '), '2026-03')
        self.assertIsNone(month_key('MARÇO'))
        self.assertIsNone(month_key('TROCAS'))

    def test_two_tabs_for_the_same_month_are_both_preserved(self):
        book = self.workbook('JANEIRO2035')
        self.block(book.active, 2, date(2035, 1, 1))
        self.assignment(book.active)
        duplicate = book.create_sheet('JANEIRO 2035')
        self.block(duplicate, 2, date(2035, 1, 1))
        self.assignment(duplicate, name='Outra Pessoa')
        other_month = book.create_sheet('NOVEMBRO2026')
        self.block(other_month, 2, date(2026, 11, 2))
        self.assignment(other_month)
        result = parse_cinderelas(book)
        self.assertEqual(result['recognized_tabs'], {'JANEIRO2035', 'JANEIRO 2035', 'NOVEMBRO2026'})
        self.assertEqual(result['preserved_tabs'], {'JANEIRO2035', 'JANEIRO 2035'})
        self.assertEqual({slot['tab'] for slot in result['slots']}, {'NOVEMBRO2026'})
        self.assertEqual(len(result['warnings']), 2)
        self.assertTrue(all('2035-01' in warning for warning in result['warnings']))

    def test_nonmonthly_tabs_and_form_swap_history_are_excluded(self):
        book = self.workbook()
        self.assignment(book.active)
        for title in ['TROCAS', 'Respostas ao formulário 1', 'ESQUELETO', 'Legendas', 'NOVEMBRO']:
            sheet = book.create_sheet(title)
            self.block(sheet, 2, date(2026, 11, 2))
            self.assignment(sheet, name='Pessoa de Outra Aba')
        book['ESQUELETO'].sheet_state = 'hidden'
        result = parse_cinderelas(book)
        self.assertEqual(result['recognized_tabs'], {'NOVEMBRO2026'})
        self.assertEqual(len(result['slots']), 1)

    def test_error_date_without_an_eligible_assignment_warns_without_discarding_month(self):
        book = self.workbook()
        self.assignment(book.active)
        book.active.cell(3, 17).value = '#VALUE!'
        self.assignment(book.active, column=17, name='Outra Pessoa', hours='8-20')
        result = parse_cinderelas(book)
        self.assertFalse(result['preserved_tabs'])
        self.assertEqual(len(result['slots']), 1)
        self.assertTrue(any('Q3' in warning for warning in result['warnings']))

    def test_eligible_name_with_invalid_date_preserves_entire_month(self):
        book = self.workbook()
        self.assignment(book.active)
        book.active.cell(3, 5).value = '#VALUE!'
        self.assignment(book.active, column=5, name='Nome Não Deve Aparecer')
        other = book.create_sheet('DEZEMBRO2026')
        self.block(other, 2, date(2026, 12, 7))
        self.assignment(other)
        result = parse_cinderelas(book)
        self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
        self.assertEqual({slot['tab'] for slot in result['slots']}, {'DEZEMBRO2026'})
        self.assertNotIn('Nome Não Deve Aparecer', '\n'.join(result['warnings']))

    def test_blank_dates_with_only_vacancies_do_not_require_an_inferred_date(self):
        book = self.workbook()
        self.assignment(book.active)
        book.active.cell(3, 5).value = None
        self.assignment(book.active, column=5, name=None)
        result = parse_cinderelas(book)
        self.assertEqual(len(result['slots']), 1)
        self.assertFalse(result['preserved_tabs'])

    def test_out_of_month_date_with_no_name_is_ignored_but_named_slot_is_preserved(self):
        book = self.workbook()
        self.assignment(book.active)
        book.active.cell(3, 5).value = datetime(2026, 10, 6)
        self.assignment(book.active, column=5, name=None)
        self.assertFalse(parse_cinderelas(book)['preserved_tabs'])
        book.active.cell(4, 6).value = 'Pessoa Fora do Mês'
        result = parse_cinderelas(book)
        self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
        self.assertEqual(result['slots'], [])

    def test_uncached_date_formula_does_not_create_or_remove_confirmed_days(self):
        book = self.workbook()
        self.assignment(book.active)
        book.active.cell(3, 2).value = '=DATE(2026,11,2)'
        result = parse_cinderelas(book)
        self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
        self.assertEqual(result['slots'], [])

    def test_uncached_name_metadata_and_formula_values_preserve_month(self):
        for formula in [False, True]:
            with self.subTest(formula=formula):
                book = self.workbook()
                self.assignment(book.active, name='=A1' if formula else None)
                if not formula:
                    book._uncached_formula_cells = {'NOVEMBRO2026': {'C4'}}
                result = parse_cinderelas(book)
                self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
                self.assertEqual(result['slots'], [])

    def test_uncached_hour_formula_does_not_silently_remove_a_named_slot(self):
        book = self.workbook()
        self.assignment(book.active)
        self.assignment(book.active, row=5, name='Outra Pessoa', hours='=A1', group=None)
        result = parse_cinderelas(book)
        self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
        self.assertEqual(result['slots'], [])

    def test_empty_malformed_and_hidden_months_are_preserved(self):
        for mode in ['empty', 'vacant', 'bad_header', 'missing_group', 'hidden']:
            with self.subTest(mode=mode):
                book = self.workbook()
                if mode != 'empty':
                    self.assignment(book.active, name=None if mode == 'vacant' else 'Pessoa Teste', group=None if mode == 'missing_group' else 'R1')
                if mode == 'bad_header':
                    book.active.cell(2, 5).value = None
                if mode == 'hidden':
                    book.active.sheet_state = 'hidden'
                result = parse_cinderelas(book)
                self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
                self.assertEqual(result['slots'], [])
                self.assertTrue(result['warnings'])

    def test_missing_month_year_format_raises_a_safe_error(self):
        with self.assertRaises(SheetFormatError):
            parse_cinderelas(self.workbook('Escala sem mês e ano'))
