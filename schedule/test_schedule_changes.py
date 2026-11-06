from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook

from .changes import notify_schedule_changes, snapshot_source
from .models import Exchange, Notification, SharedActivity, Shift, SyncState
from .sheets import SheetFormatError, resident_for, sync_workbook


class PersonalScheduleChangeTests(TestCase):
    def setUp(self):
        self.day = (timezone.localtime() + timedelta(days=20)).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None,
        )
        self.source = 'personal-change-source'
        self.activities = [
            ('Ana Teste', '8-12', 'Ambulatório'),
            ('Bia Teste', '13-18', 'Patologia'),
            ('Carla Teste', '19-22', 'Observação'),
        ]

    def workbook(self, activities=None, day=None):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Espelho Semanal'
        sheet.append([None, day or self.day])
        sheet.append([None, 'Segunda'])
        for names, hours, sector in self.activities if activities is None else activities:
            if isinstance(names, str):
                names = [names]
            sheet.append(['INSTITUTO', sector])
            sheet.append([None, 'R1', hours, None, None, *names])
        return book

    def test_initial_import_and_identical_cycles_are_silent(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        self.assertTrue(SyncState.objects.get(source=self.source).last_success)
        for _ in range(3):
            sync_workbook(book, self.source)
        self.assertFalse(Notification.objects.exists())

    def test_only_affected_resident_receives_before_and_after(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        book.active.cell(row=4, column=3).value = '9-13'
        book.active.cell(row=3, column=2).value = 'Centro cirúrgico'
        sync_workbook(book, self.source)
        notification = Notification.objects.get()
        self.assertEqual(notification.resident, resident_for('Ana Teste'))
        self.assertEqual(notification.kind, 'schedule')
        self.assertEqual(notification.payload['counts'], {
            'added': 0, 'removed': 0, 'updated': 1, 'total': 1,
        })
        change = notification.payload['changes'][0]
        self.assertEqual(change['before']['sector'], 'INSTITUTO · Ambulatório')
        self.assertEqual(change['after']['sector'], 'INSTITUTO · Centro cirúrgico')
        self.assertIn('08:00', change['before']['starts_at'])
        self.assertIn('09:00', change['after']['starts_at'])
        self.assertIn('Antes:', notification.message)
        self.assertIn('Agora:', notification.message)
        self.assertIn('scope=mine', notification.link)
        week = self.day.date() - timedelta(days=self.day.weekday())
        self.assertIn(f'week={week.isoformat()}', notification.link)
        sync_workbook(book, self.source)
        self.assertEqual(Notification.objects.count(), 1)

    def test_change_of_owner_notifies_old_and_new_resident_only(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        book.active.cell(row=4, column=6).value = 'Bia Teste'
        sync_workbook(book, self.source)
        self.assertEqual(set(Notification.objects.values_list('resident_id', flat=True)), {
            resident_for('Ana Teste').pk, resident_for('Bia Teste').pk,
        })
        self.assertEqual(Notification.objects.get(resident=resident_for('Ana Teste')).payload['counts']['removed'], 1)
        self.assertEqual(Notification.objects.get(resident=resident_for('Bia Teste')).payload['counts']['added'], 1)

    def test_pair_membership_change_notifies_all_affected_members(self):
        book = self.workbook([
            (('Ana Teste', 'Bia Teste'), '8-12', 'Ambulatório'),
            ('Carla Teste', '13-18', 'Patologia'),
        ])
        sync_workbook(book, self.source)
        book.active.cell(row=4, column=7).value = 'Dora Teste'
        sync_workbook(book, self.source)
        self.assertEqual(set(Notification.objects.values_list('resident_id', flat=True)), {
            resident_for('Ana Teste').pk, resident_for('Bia Teste').pk,
            resident_for('Dora Teste').pk,
        })
        ana = Notification.objects.get(resident=resident_for('Ana Teste'))
        item = ana.payload['changes'][0]
        self.assertTrue(item['participants_changed'])
        self.assertNotIn('_participants', item['before'])
        self.assertNotIn('_participants', item['after'])
        self.assertNotIn('Bia', ana.message)
        self.assertNotIn('Dora', ana.message)

    def test_incomplete_time_change_and_resolution_are_notified(self):
        book = self.workbook([('Ana Teste', None, 'Ambulatório')])
        sync_workbook(book, self.source)
        book.active.cell(row=4, column=3).value = 'A confirmar'
        sync_workbook(book, self.source)
        first = Notification.objects.get()
        self.assertEqual(first.payload['changes'][0]['before']['raw_time'], '')
        self.assertEqual(first.payload['changes'][0]['after']['raw_time'], 'A confirmar')
        book.active.cell(row=4, column=3).value = '8-12'
        sync_workbook(book, self.source)
        second = Notification.objects.order_by('pk').last()
        self.assertEqual(second.payload['changes'][0]['before']['kind'], 'incomplete')
        self.assertEqual(second.payload['changes'][0]['after']['kind'], 'shift')
        self.assertEqual(Notification.objects.count(), 2)

    def test_removed_and_added_entries_notify_the_respective_residents(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        book.active.cell(row=4, column=6).value = None
        book.active.append(['INSTITUTO', 'Novo setor'])
        book.active.append([None, 'R1', '10-12', None, None, 'Dora Teste'])
        sync_workbook(book, self.source)
        ana = Notification.objects.get(resident=resident_for('Ana Teste'))
        dora = Notification.objects.get(resident=resident_for('Dora Teste'))
        self.assertEqual(ana.payload['counts']['removed'], 1)
        self.assertEqual(dora.payload['counts']['added'], 1)
        self.assertEqual(Notification.objects.count(), 2)

    def test_historical_import_changes_are_silent(self):
        book = self.workbook([
            ('Ana Teste', '8-12', 'Ambulatório'),
            ('Bia Teste', None, 'Patologia'),
        ], day=self.day - timedelta(days=60))
        sync_workbook(book, self.source)
        book.active.cell(row=4, column=3).value = '9-13'
        book.active.cell(row=6, column=3).value = '14-18'
        sync_workbook(book, self.source)
        self.assertFalse(Notification.objects.exists())

    def test_row_reordering_without_calendar_changes_is_silent(self):
        sync_workbook(self.workbook(), self.source)
        sync_workbook(self.workbook(list(reversed(self.activities))), self.source)
        self.assertFalse(Notification.objects.exists())

    def test_failed_parser_rolls_back_partial_import_without_notification(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        before = list(Shift.objects.order_by('pk').values())
        last_success = SyncState.objects.get(source=self.source).last_success
        book.active.cell(row=4, column=3).value = '9-13'
        # A second day without a sector fails after the first day was processed.
        book.active.cell(row=1, column=9).value = self.day + timedelta(days=1)
        book.active.cell(row=4, column=10).value = '8-12'
        book.active.cell(row=4, column=13).value = 'Dora Teste'
        with self.assertRaises(SheetFormatError):
            sync_workbook(book, self.source)
        self.assertEqual(list(Shift.objects.order_by('pk').values()), before)
        self.assertEqual(SyncState.objects.get(source=self.source).last_success, last_success)
        self.assertFalse(Notification.objects.exists())

    def test_notification_error_rolls_back_import_and_notification_together(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        before = list(Shift.objects.order_by('pk').values())
        book.active.cell(row=4, column=3).value = '9-13'

        def record_then_fail(*args, **kwargs):
            notify_schedule_changes(*args, **kwargs)
            self.assertTrue(Notification.objects.exists())
            raise RuntimeError('notification failure')

        with patch('schedule.changes.notify_schedule_changes', side_effect=record_then_fail):
            with self.assertRaises(RuntimeError):
                sync_workbook(book, self.source)
        self.assertEqual(list(Shift.objects.order_by('pk').values()), before)
        self.assertFalse(Notification.objects.exists())

    def test_accepted_exchange_preserved_by_import_is_not_a_new_change(self):
        book = self.workbook()
        sync_workbook(book, self.source)
        ana = resident_for('Ana Teste')
        bia = resident_for('Bia Teste')
        first = Shift.objects.get(source=self.source, resident=ana)
        second = Shift.objects.get(source=self.source, resident=bia)
        first.resident = bia
        second.resident = ana
        first.save(update_fields=['resident'])
        second.save(update_fields=['resident'])
        Exchange.objects.create(
            offered=first, requested=second, proposer=ana, recipient=bia,
            status='accepted', resolved_at=timezone.now(),
        )
        sync_workbook(book, self.source)
        first.refresh_from_db()
        self.assertEqual(first.resident, bia)
        self.assertFalse(Notification.objects.exists())

    def test_allocation_status_is_part_of_shared_snapshot(self):
        book = self.workbook([(('Ana Teste', 'Bia Teste'), '8-12', 'Ambulatório')])
        sync_workbook(book, self.source)
        now = timezone.now()
        before = snapshot_source(self.source, now=now)
        activity = SharedActivity.objects.get(source=self.source)
        activity.allocated_to = resident_for('Ana Teste')
        activity.save(update_fields=['allocated_to'])
        after = snapshot_source(self.source, now=now)
        previous_success = SyncState.objects.get(source=self.source).last_success
        notify_schedule_changes(self.source, before, after, previous_success=previous_success)
        self.assertEqual(Notification.objects.count(), 2)
        ana = Notification.objects.get(resident=resident_for('Ana Teste'))
        self.assertEqual(ana.payload['changes'][0]['after']['status'], 'allocated_to_you')
        # Replaying exactly the same successful event cannot duplicate its rows.
        notify_schedule_changes(self.source, before, after, previous_success=previous_success)
        self.assertEqual(Notification.objects.count(), 2)

    def test_large_change_payload_is_bounded_with_complete_counts(self):
        sync_workbook(self.workbook([('Ana Teste', '8-12', 'Ambulatório')]), self.source)
        activities = [('Ana Teste', '8-12', 'Ambulatório')]
        activities += [('Ana Teste', '13-18', f'Setor {number}') for number in range(30)]
        sync_workbook(self.workbook(activities), self.source)
        notification = Notification.objects.get()
        self.assertEqual(notification.payload['counts']['added'], 30)
        self.assertEqual(notification.payload['counts']['total'], 30)
        self.assertEqual(len(notification.payload['changes']), 20)
        self.assertTrue(notification.payload['truncated'])
