from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook
from openpyxl.styles import Color, Font, PatternFill

from .models import Exchange, IncompleteActivity, Notification, SharedActivity, Shift
from .services import propose_allocation, respond_allocation
from .sheets import load_export, sync_workbook
from .source_metadata import color_hex, heading_colors, right_note, theme_palette


THEME = b'''<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements><a:clrScheme name="Fixture">
<a:dk1><a:sysClr val="windowText" lastClr="123456"/></a:dk1>
<a:lt1><a:sysClr val="window" lastClr="FEDCBA"/></a:lt1>
<a:dk2><a:srgbClr val="112233"/></a:dk2><a:lt2><a:srgbClr val="ABCDEF"/></a:lt2>
<a:accent1><a:srgbClr val="0B5394"/></a:accent1>
</a:clrScheme></a:themeElements></a:theme>'''


class HeadingColorTests(SimpleTestCase):
    def test_rgb_indexed_theme_and_tint_decode_without_xml_order_assumption(self):
        palette = theme_palette(SimpleNamespace(loaded_theme=THEME))
        self.assertEqual(color_hex(Color(rgb='FF0B5394'), palette), '#0B5394')
        self.assertEqual(color_hex(Color(indexed=10), palette), '#FF0000')
        self.assertEqual(color_hex(Color(theme=0), palette), '#FEDCBA')
        self.assertEqual(color_hex(Color(theme=1), palette), '#123456')
        self.assertEqual(color_hex(Color(theme=2), palette), '#ABCDEF')
        self.assertEqual(color_hex(Color(theme=3), palette), '#112233')
        self.assertEqual(color_hex(Color(theme=4), palette), '#0B5394')
        self.assertEqual(color_hex(Color(rgb='000000', tint=.5), palette), '#808080')
        self.assertEqual(color_hex(Color(rgb='FFFFFF', tint=-.5), palette), '#808080')

    def test_auto_and_reserved_palette_colors_do_not_become_css(self):
        for color in [Color(auto=True), Color(indexed=64), Color(indexed=65), Color(theme=11)]:
            with self.subTest(color=color):
                self.assertEqual(color_hex(color, {}), '')

    def test_unfilled_heading_is_explicit_white_with_safe_black_font(self):
        book = Workbook()
        cell = book.active['B3']
        cell.value = 'Setor'
        cell.fill = PatternFill(patternType='darkGrid', fgColor='AA0000')
        cell.font = Font(color=Color(auto=True))
        self.assertEqual(heading_colors(cell, {}), {
            'source_background': '#FFFFFF', 'source_foreground': '#000000',
        })

    def test_note_offset_is_per_day_and_bounded(self):
        row = [None] * 22
        row[7] = '  Cob R1  '
        row[14] = 'INTERCONSULTA\nHVL'
        row[21] = 'x' * 2200
        self.assertEqual(right_note(row, 1), 'Cob R1')
        self.assertEqual(right_note(row, 8), 'INTERCONSULTA\nHVL')
        self.assertEqual(len(right_note(row, 15)), 2000)
        self.assertEqual(right_note([None] * 8, 1), '')
        row[7] = True
        self.assertEqual(right_note(row, 1), '')


class WeeklySourceMetadataTests(TestCase):
    source = 'source-metadata-fixture'

    def workbook(self):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Espelho Semanal'
        for index, (fill, note) in enumerate([
            ('0B5394', 'Cob R1'), ('CC0000', 'INTERCONSULTA - HVL'), ('6AA84F', 'Sala Rita'),
        ]):
            column = 2 + 7 * index
            sheet.cell(1, column, datetime(2035, 1, 1) + timedelta(days=index))
            sheet.cell(2, column, ['Segunda', 'Terça', 'Quarta'][index])
            sheet.cell(3, column, 'Ambulatório')
            sheet.merge_cells(start_row=3, start_column=column, end_row=3, end_column=column+5)
            sheet.cell(3, column).fill = PatternFill('solid', fgColor=fill)
            sheet.cell(3, column).font = Font(color='FFFFFF')
            for row, hours, first, second in [
                (4, '8-12', f'Pessoa Definida {index}', None),
                (5, '13-17', f'Pessoa Dupla A {index}', f'Pessoa Dupla B {index}'),
                (6, 'A confirmar', f'Pessoa Pendente {index}', None),
            ]:
                sheet.cell(row, column, 'R1')
                sheet.cell(row, column+1, hours)
                sheet.cell(row, column+4, first)
                if second: sheet.cell(row, column+5, second)
                sheet.cell(row, column+6, note + f' linha {row}')
                # Alternating row shading is deliberately unrelated to the heading.
                sheet.cell(row, column+4).fill = PatternFill('solid', fgColor='EEEEDD')
                sheet.cell(row, column+4).font = Font(color='0000FF')
        sheet['A3'] = 'INSTITUTO'
        return book

    def test_original_heading_colors_and_right_notes_reach_all_activity_types(self):
        sync_workbook(self.workbook(), self.source)
        for index, (fill, note) in enumerate([
            ('#0B5394', 'Cob R1'), ('#CC0000', 'INTERCONSULTA - HVL'), ('#6AA84F', 'Sala Rita'),
        ]):
            day = datetime(2035, 1, 1).date() + timedelta(days=index)
            records = [
                Shift.objects.get(source=self.source, starts_at__date=day),
                SharedActivity.objects.get(source=self.source, starts_at__date=day),
                IncompleteActivity.objects.get(source=self.source, date=day),
            ]
            for row, record in zip([4, 5, 6], records):
                with self.subTest(day=day, kind=type(record).__name__):
                    self.assertEqual(record.source_background, fill)
                    self.assertEqual(record.source_foreground, '#FFFFFF')
                    self.assertEqual(record.notes, note + f' linha {row}')

    def test_read_only_export_preserves_merged_heading_styles_and_notes(self):
        original = self.workbook()
        stream = BytesIO()
        original.save(stream)
        book = load_export(stream.getvalue())
        try:
            sync_workbook(book, self.source)
        finally:
            book.close()
        shift = Shift.objects.get(source=self.source, source_cell='C4:G4')
        self.assertEqual(shift.source_background, '#0B5394')
        self.assertEqual(shift.source_foreground, '#FFFFFF')
        self.assertEqual(shift.notes, 'Cob R1 linha 4')

    def test_heading_in_c_is_used_and_next_unfilled_section_does_not_inherit_color(self):
        book = self.workbook()
        sheet = book.active
        sheet['B7'] = 'R1'
        sheet['C7'] = 'UNEX'
        sheet.merge_cells('C7:G7')
        sheet['C7'].fill = PatternFill('solid', fgColor='EFEFEF')
        sheet['C7'].font = Font(color='000000')
        sheet.append([None, 'R1', '17-18', None, None, 'Pessoa UNEX', None, 'Cob UNEX'])
        sheet['B9'] = 'Patologia'
        sheet.append([None, 'R1', '18-19', None, None, 'Pessoa Sem Cor'])
        sync_workbook(book, self.source)
        unex = Shift.objects.get(source=self.source, source_cell='C8:G8')
        plain = Shift.objects.get(source=self.source, source_cell='C10:G10')
        self.assertEqual(unex.sector, 'INSTITUTO · UNEX')
        self.assertEqual((unex.source_background, unex.source_foreground), ('#EFEFEF', '#000000'))
        self.assertEqual(unex.notes, 'Cob UNEX')
        self.assertEqual((plain.source_background, plain.source_foreground), ('#FFFFFF', '#000000'))
        self.assertEqual(plain.notes, '')

    def test_metadata_edit_and_clearing_preserve_ids_and_owners(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        before = {
            model: {obj.source_cell: (obj.pk, obj.external_id) for obj in model.objects.filter(source=self.source)}
            for model in [Shift, SharedActivity, IncompleteActivity]
        }
        sheet = book.active
        sheet['B3'].fill = PatternFill('solid', fgColor='A64D79')
        sheet['B3'].font = Font(color='010203')
        for row in [4, 5, 6]: sheet.cell(row, 8).value = None
        sync_workbook(book, self.source)
        for model in [Shift, SharedActivity, IncompleteActivity]:
            self.assertEqual(before[model], {
                obj.source_cell: (obj.pk, obj.external_id) for obj in model.objects.filter(source=self.source)
            })
            monday = model.objects.get(source=self.source, source_cell__startswith='C')
            self.assertEqual(monday.source_background, '#A64D79')
            self.assertEqual(monday.source_foreground, '#010203')
            self.assertEqual(monday.notes, '')
        self.assertEqual(Shift.objects.filter(source=self.source, active=True).count(), 3)

    def test_accepted_swap_keeps_owner_but_refreshes_source_metadata(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        offered = Shift.objects.get(source=self.source, source_cell='C4:G4')
        requested = Shift.objects.get(source=self.source, source_cell='J4:N4')
        proposer, recipient = offered.resident, requested.resident
        offered.resident, requested.resident = recipient, proposer
        offered.save(update_fields=['resident'])
        requested.save(update_fields=['resident'])
        Exchange.objects.create(offered=offered, requested=requested, proposer=proposer, recipient=recipient, status='accepted')
        book.active['B3'].fill = PatternFill('solid', fgColor='A64D79')
        book.active['H4'] = 'Nova cobertura'
        sync_workbook(book, self.source)
        offered.refresh_from_db()
        requested.refresh_from_db()
        self.assertEqual(offered.resident_id, recipient.pk)
        self.assertEqual(requested.resident_id, proposer.pk)
        self.assertEqual(offered.source_background, '#A64D79')
        self.assertEqual(offered.notes, 'Nova cobertura')
        self.assertEqual(Shift.objects.filter(source=self.source, active=True).count(), 3)

    def test_note_changes_notify_affected_owner_and_color_changes_are_silent(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        self.assertFalse(Notification.objects.exists())
        book.active['B3'].fill = PatternFill('solid', fgColor='A64D79')
        sync_workbook(book, self.source)
        self.assertFalse(Notification.objects.exists())
        shift = Shift.objects.get(source=self.source, source_cell='C4:G4')
        book.active['H4'] = 'INTERCONSULTA - HVL'
        sync_workbook(book, self.source)
        notification = Notification.objects.get()
        self.assertEqual(notification.resident_id, shift.resident_id)
        self.assertIn('Cob R1 linha 4', notification.message)
        self.assertIn('INTERCONSULTA - HVL', notification.message)
        change = notification.payload['changes'][0]
        self.assertEqual(change['before']['notes'], 'Cob R1 linha 4')
        self.assertEqual(change['after']['notes'], 'INTERCONSULTA - HVL')
        sync_workbook(book, self.source)
        self.assertEqual(Notification.objects.count(), 1)

    def test_accepted_division_inherits_metadata_and_refresh_backfills_existing_final_shifts(self):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Espelho Semanal'
        sheet['B1'] = datetime(2035, 1, 1)
        sheet['B2'] = 'Segunda'
        sheet['A3'] = 'INSTITUTO'
        for heading_row, sector, background, note in [
            (3, 'Ambulatório', '0B5394', 'Cob R1'),
            (5, 'Centro Cirúrgico', 'CC0000', 'INTERCONSULTA - HVL'),
        ]:
            row = heading_row + 1
            sheet.cell(heading_row, 2, sector)
            sheet.cell(heading_row, 2).fill = PatternFill('solid', fgColor=background)
            sheet.cell(heading_row, 2).font = Font(color='FFFFFF')
            sheet.cell(row, 2, 'R1')
            sheet.cell(row, 3, '8-12')
            sheet.cell(row, 6, 'Pessoa Dupla Um')
            sheet.cell(row, 7, 'Pessoa Dupla Dois')
            sheet.cell(row, 8, note)
        sync_workbook(book, self.source)
        mine = SharedActivity.objects.get(source=self.source, source_cell='C4:G4')
        theirs = SharedActivity.objects.get(source=self.source, source_cell='C6:G6')
        proposer, recipient = list(mine.residents.order_by('pk'))
        proposal = propose_allocation(proposer, mine.pk, theirs.pk)
        respond_allocation(recipient, proposal.pk, 'accept')
        finals = [Shift.objects.get(external_id=f'allocation:{proposal.pk}:{item.pk}') for item in [mine, theirs]]
        expected = [('#0B5394', '#FFFFFF', 'Cob R1'), ('#CC0000', '#FFFFFF', 'INTERCONSULTA - HVL')]
        for final, metadata in zip(finals, expected):
            self.assertEqual((final.source_background, final.source_foreground, final.notes), metadata)
        self.assertEqual([item.resident_id for item in finals], [proposer.pk, recipient.pk])
        ids = [item.pk for item in finals]
        # An installation upgraded after accepting the division has blank new
        # fields. The next normal import must enrich its existing final slots.
        Shift.objects.filter(pk__in=ids).update(source_background='', source_foreground='', notes='')
        sync_workbook(book, self.source)
        for final, metadata in zip(finals, expected):
            final.refresh_from_db()
            self.assertEqual((final.source_background, final.source_foreground, final.notes), metadata)
            self.assertTrue(final.active)
        self.assertEqual(list(Shift.objects.order_by('pk').values_list('pk', flat=True)), ids)
        sheet['B3'].fill = PatternFill('solid', fgColor='A64D79')
        sheet['H4'] = 'Nova cobertura R1'
        sync_workbook(book, self.source)
        finals[0].refresh_from_db()
        self.assertEqual(finals[0].source_background, '#A64D79')
        self.assertEqual(finals[0].notes, 'Nova cobertura R1')
        self.assertEqual(finals[0].resident_id, proposer.pk)
