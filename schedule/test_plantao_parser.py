from datetime import date, datetime, timedelta, timezone as utc
from types import SimpleNamespace

from django.test import SimpleTestCase
from openpyxl import Workbook

from .cinderelas_parser import DAY_COLUMNS, WEEKDAYS, parse_cinderelas
from .plantao_parser import parse_plantao_hours, parse_plantoes
from .sheets import SheetFormatError


class PlantaoParserTests(SimpleTestCase):
    def workbook(self, title='NOVEMBRO2026', monday=date(2026, 11, 2)):
        book = Workbook()
        book.active.title = title
        self.block(book.active, 2, monday)
        return book

    def block(self, sheet, row, monday):
        for offset, (column, weekday) in enumerate(zip(DAY_COLUMNS, WEEKDAYS)):
            sheet.cell(row, column, weekday)
            sheet.cell(row + 1, column, datetime.combine(monday + timedelta(days=offset), datetime.min.time()))

    def assignment(self, sheet, row=4, column=2, hours='18-24', name='Pessoa Fictícia', group='R1'):
        if group:
            sheet.cell(row, 1, group)
        sheet.cell(row, column, hours)
        if name is not None:
            sheet.cell(row, column + 1, name)

    def test_all_explicit_grid_hours_are_imported_with_their_category(self):
        book = self.workbook()
        expected = [
            ('18-8', 'night', 14), ('8-8', 'night', 24),
            ('8-20', 'weekend', 12), ('9-21', 'weekend', 12),
            ('12-24', 'weekend', 12), ('10-18', 'weekend', 8),
            ('10-16', 'weekend', 6), ('18-24', 'cinderela', 6),
        ]
        for row, (hours, category, duration) in enumerate(expected, 4):
            self.assignment(book.active, row=row, hours=hours)
        result = parse_plantoes(book)
        self.assertEqual(result['preserved_tabs'], set())
        self.assertEqual(len(result['slots']), len(expected))
        for slot, (hours, category, duration) in zip(result['slots'], expected):
            with self.subTest(hours=hours):
                self.assertEqual(slot['normalized_hours'], hours)
                self.assertEqual(slot['category'], category)
                self.assertEqual(slot['ends_at'] - slot['starts_at'], timedelta(hours=duration))
                self.assertEqual(slot['slot_key'], f'NOVEMBRO2026|2026-11-02|R1|{hours}|1')

    def test_overnight_and_24_hour_intervals_end_on_the_following_local_day(self):
        for hours, end_hour, duration in [('18-8', 8, 14), ('8-8', 8, 24), ('18-24', 0, 6), ('12-24', 0, 12)]:
            with self.subTest(hours=hours):
                book = self.workbook()
                self.assignment(book.active, hours=hours)
                slot = parse_plantoes(book)['slots'][0]
                self.assertEqual(slot['ends_at'].date(), date(2026, 11, 3))
                self.assertEqual(slot['ends_at'].hour, end_hour)
                self.assertEqual(slot['ends_at'].astimezone(utc.utc) - slot['starts_at'].astimezone(utc.utc), timedelta(hours=duration))

    def test_equivalent_midnight_formats_keep_old_cinderela_slot_identity(self):
        for hours in ['18-24', '18:00 – 00:00', '18:00—24:00']:
            with self.subTest(hours=hours):
                book = self.workbook()
                self.assignment(book.active, hours=hours)
                old = parse_cinderelas(book)['slots'][0]
                new = parse_plantoes(book)['slots'][0]
                for key in old:
                    self.assertEqual(new[key], old[key])
                self.assertEqual(new['category'], 'cinderela')
                self.assertEqual(new['normalized_hours'], '18-24')

    def test_normalized_hours_support_minutes_and_equal_minutes_mean_next_day(self):
        for value, normalized, category in [
            ('08:00 – 20:00', '8-20', 'weekend'),
            ('18:30-08:15', '18:30-8:15', 'night'),
            ('10:15-10:15', '10:15-10:15', 'night'),
            ('12:00-00:00', '12-24', 'weekend'),
        ]:
            with self.subTest(value=value):
                result = parse_plantao_hours(value)
                self.assertEqual(result['normalized_hours'], normalized)
                self.assertEqual(result['category'], category)

    def test_ordinals_are_independent_per_time_and_count_vacancies_and_markers(self):
        book = self.workbook()
        for row, hours, name in [
            (4, '8-20', None), (5, '18-24', None),
            (6, '18-8', 'COB UNEX'), (7, '18-24', 'x'),
            (8, '8-20', 'Pessoa Diurna'), (9, '18-8', 'Pessoa Noturna'),
            (10, '18-24', 'Pessoa Cinderela'),
        ]:
            self.assignment(book.active, row=row, hours=hours, name=name, group='R1' if row == 4 else None)
        result = parse_plantoes(book)
        self.assertEqual([slot['slot_key'] for slot in result['slots']], [
            'NOVEMBRO2026|2026-11-02|R1|8-20|2',
            'NOVEMBRO2026|2026-11-02|R1|18-8|2',
            'NOVEMBRO2026|2026-11-02|R1|18-24|3',
        ])
        self.assertEqual(len(result['warnings']), 2)

    def test_group_and_date_separate_identical_time_slot_ordinals(self):
        book = self.workbook()
        self.assignment(book.active, hours='18-8')
        self.assignment(book.active, row=5, group='R2', hours='18-8')
        self.assignment(book.active, row=6, group='R3', hours='18-8')
        self.assignment(book.active, row=6, column=5, group=None, hours='18-8')
        result = parse_plantoes(book)
        self.assertEqual(len(result['slots']), 4)
        self.assertTrue(all(slot['slot_key'].endswith('|1') for slot in result['slots']))
        self.assertEqual({slot['group'] for slot in result['slots']}, {'R1', 'R2', 'R3'})

    def test_physical_row_or_owner_changes_do_not_change_logical_slot(self):
        book = self.workbook()
        self.assignment(book.active, hours='18-8')
        first = parse_plantoes(book)['slots'][0]
        book.active.cell(4, 3).value = 'Outra Pessoa Fictícia'
        book.active.insert_rows(4, 2)
        second = parse_plantoes(book)['slots'][0]
        self.assertEqual(first['slot_key'], second['slot_key'])
        self.assertNotEqual(first['cell'], second['cell'])
        self.assertNotEqual(first['name'], second['name'])

    def test_daytime_duty_is_included_on_weekdays_without_guessing_holidays(self):
        book = self.workbook()
        self.assignment(book.active, hours='10-16')
        slot = parse_plantoes(book)['slots'][0]
        self.assertEqual(slot['date'].weekday(), 0)
        self.assertEqual(slot['category'], 'weekend')

    def test_named_daytime_slot_on_invalid_date_preserves_entire_month(self):
        book = self.workbook('MAIO2026', date(2026, 5, 4))
        self.assignment(book.active)
        self.block(book.active, 62, date(2026, 5, 18))
        book.active.cell(63, 17).value = '#VALUE!'
        self.assignment(book.active, row=64, column=17, hours='8-20')
        result = parse_plantoes(book)
        self.assertEqual(result['recognized_tabs'], {'MAIO2026'})
        self.assertEqual(result['preserved_tabs'], {'MAIO2026'})
        self.assertEqual(result['slots'], [])
        self.assertTrue(any('Q63' in warning for warning in result['warnings']))
        self.assertTrue(any('R64' in warning and 'data inválida' in warning for warning in result['warnings']))

    def test_placeholder_or_vacancy_without_date_does_not_invent_a_shift(self):
        book = self.workbook()
        self.assignment(book.active, hours='8-20')
        book.active.cell(3, 5).value = None
        self.assignment(book.active, column=5, name='COB UNEX', hours='18-8')
        self.assignment(book.active, row=5, column=5, group=None, name='x', hours='8-8')
        self.assignment(book.active, row=6, column=5, group=None, name=None, hours='8-20')
        result = parse_plantoes(book)
        self.assertEqual(len(result['slots']), 1)
        self.assertEqual(result['preserved_tabs'], set())

    def test_cached_formula_metadata_for_new_time_rows_preserves_month(self):
        for coordinate, value in [('B4', None), ('C4', None), ('C4', '=A1'), ('B4', '=A1')]:
            with self.subTest(coordinate=coordinate, value=value):
                book = self.workbook()
                self.assignment(book.active, hours='8-20')
                book.active[coordinate].value = value
                book._uncached_formula_cells = {'NOVEMBRO2026': {coordinate}}
                result = parse_plantoes(book)
                self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
                self.assertEqual(result['slots'], [])

    def test_invalid_or_absent_hours_with_a_name_are_not_silently_removed(self):
        for value in ['25-8', '24-24', '8:60-20', '12-24:01', '8-25', '018-24', 'sem horário', None]:
            with self.subTest(value=value):
                book = self.workbook()
                self.assignment(book.active)
                self.assignment(book.active, row=5, group=None, hours=value)
                result = parse_plantoes(book)
                self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
                self.assertEqual(result['slots'], [])

    def test_duplicate_month_tabs_are_both_held_while_valid_month_is_imported(self):
        book = self.workbook()
        self.assignment(book.active, hours='18-8')
        duplicate = book.create_sheet('NOVEMBRO 2026')
        self.block(duplicate, 2, date(2026, 11, 2))
        self.assignment(duplicate, hours='8-8')
        other = book.create_sheet('DEZEMBRO2026')
        self.block(other, 2, date(2026, 12, 7))
        self.assignment(other, hours='8-20')
        result = parse_plantoes(book)
        self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026', 'NOVEMBRO 2026'})
        self.assertEqual({slot['tab'] for slot in result['slots']}, {'DEZEMBRO2026'})

    def test_hidden_or_incomplete_months_remain_preserved(self):
        for mode in ['hidden', 'incomplete_header', 'missing_group', 'bad_name']:
            with self.subTest(mode=mode):
                book = self.workbook()
                self.assignment(book.active, hours='18-8', group=None if mode == 'missing_group' else 'R1')
                if mode == 'hidden':
                    book.active.sheet_state = 'hidden'
                elif mode == 'incomplete_header':
                    book.active.cell(2, 5).value = None
                elif mode == 'bad_name':
                    book.active.cell(4, 3).value = '#VALUE!'
                result = parse_plantoes(book)
                self.assertEqual(result['preserved_tabs'], {'NOVEMBRO2026'})
                self.assertEqual(result['slots'], [])

    def test_nonmonthly_tabs_are_ignored_and_explicit_year_is_required(self):
        for title in ['TROCAS', 'ESQUELETO', 'NOVEMBRO']:
            with self.subTest(title=title):
                with self.assertRaises(SheetFormatError):
                    parse_plantoes(self.workbook(title))
        book = self.workbook()
        self.assignment(book.active, hours='8-20')
        ignored = book.create_sheet('Respostas ao formulário 1')
        self.block(ignored, 2, date(2026, 11, 2))
        self.assignment(ignored, hours='8-8')
        result = parse_plantoes(book)
        self.assertEqual(len(result['slots']), 1)

    def test_accented_month_title_keeps_source_tab_spelling(self):
        book = self.workbook(monday=date(2026, 3, 2))
        self.assignment(book.active, hours='8-20')
        sheet = SimpleNamespace(title=' março / 2026 ', sheet_state='visible', iter_rows=book.active.iter_rows)
        result = parse_plantoes([sheet])
        self.assertEqual(result['slots'][0]['tab'], ' março / 2026 ')
        self.assertEqual(result['slots'][0]['date'], date(2026, 3, 2))

    def test_legacy_parser_still_ignores_other_explicit_hours(self):
        book = self.workbook()
        self.assignment(book.active)
        self.assignment(book.active, row=5, hours='8-20', group=None)
        self.assertEqual(len(parse_cinderelas(book)['slots']), 1)
        self.assertEqual(len(parse_plantoes(book)['slots']), 2)
