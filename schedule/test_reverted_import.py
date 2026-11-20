"""A fresh API-confirmed undo must survive Google's delayed XLSX export."""
from unittest.mock import patch

from django.test import TestCase

from . import test_sheets_write as pilot
from .models import Exchange, Shift
from .sheets import sync_workbook
from .sheets_reconciliation import reconcile_confirmed_writes
from .sheets_write import apply_preview, preview_exchange


class RevertedWeeklyImportTests(TestCase):
    def setUp(self):
        pilot.WeeklyWritePilotTests.setUp(self)
        self.confirmed=apply_preview(self.admin,preview_exchange(self.admin,self.exchange.pk).pk)
        # First acknowledge the successful write in XLSX: both identities are
        # rekeyed to their swapped owners before the editor undoes the swap.
        self.book.active['F4']='Pessoa Dois'
        self.book.active['M4']='Pessoa Um'
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.ids=(self.offered.pk,self.requested.pk)
        self.count=Shift.objects.count()
        self.google.cell(3,5)['effectiveValue']={'stringValue':'Pessoa Um'}
        self.google.cell(3,12)['effectiveValue']={'stringValue':'Pessoa Dois'}
        with patch('schedule.sheets_reconciliation.authorized_session',return_value=self.google):
            result=reconcile_confirmed_writes(self.admin)
        self.assertEqual(result.reverted,1)

    def owners(self):
        return list(Shift.objects.filter(pk__in=self.ids).order_by('pk').values_list('resident_id',flat=True))

    def test_old_export_cannot_reapply_swapped_owners_then_acknowledges_same_event_ids(self):
        for _ in range(3):
            sync_workbook(self.book,self.source.spreadsheet_id)
            self.assertEqual(self.owners(),[self.a.pk,self.b.pk])
            self.assertEqual(Shift.objects.count(),self.count)
            self.assertTrue(all(shift.active for shift in Shift.objects.filter(pk__in=self.ids)))
            self.assertTrue(all(shift.source_snapshot['google_revert_export_pending']
                                for shift in Shift.objects.filter(pk__in=self.ids)))
        self.book.active['F4']='Pessoa Um';self.book.active['M4']='Pessoa Dois'
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.assertEqual(self.owners(),[self.a.pk,self.b.pk])
        self.assertEqual(Shift.objects.count(),self.count)
        for shift in Shift.objects.filter(pk__in=self.ids):
            self.assertNotIn('google_revert_export_pending',shift.source_snapshot)
            self.assertNotIn('google_revert_previous_owner',shift.source_snapshot)
        self.assertEqual(Exchange.objects.get(pk=self.exchange.pk).status,'reverted')

    def test_after_export_acknowledges_undo_later_source_edits_are_not_frozen(self):
        self.book.active['F4']='Pessoa Um';self.book.active['M4']='Pessoa Dois'
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.book.active['F4']='Pessoa Nova'
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.offered.refresh_from_db()
        self.assertFalse(self.offered.active)
        self.assertEqual(Shift.objects.filter(source=self.source.spreadsheet_id,active=True).count(),2)
        self.assertTrue(Shift.objects.filter(source=self.source.spreadsheet_id,active=True,
                                            resident__first_name='Pessoa Nova').exists())

    def test_unknown_export_name_before_acknowledgement_preserves_undo_and_flags_conflict(self):
        self.book.active['F4']='Pessoa Nova'
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.offered.refresh_from_db()
        self.assertEqual(self.offered.resident_id,self.a.pk)
        self.assertTrue(self.offered.requires_reconciliation)
        self.assertEqual(Shift.objects.count(),self.count)

    def test_changed_export_structure_keeps_restored_identity_for_reconciliation(self):
        for address,value in [('G4','Pessoa Nova'),('C4','A confirmar'),('C4','9-13'),('F4',None)]:
            with self.subTest(address=address,value=value):
                before=self.book.active[address].value
                self.book.active[address]=value
                sync_workbook(self.book,self.source.spreadsheet_id)
                self.offered.refresh_from_db()
                self.assertTrue(self.offered.active)
                self.assertTrue(self.offered.requires_reconciliation)
                self.assertEqual(self.offered.resident_id,self.a.pk)
                self.assertEqual(Shift.objects.count(),self.count)
                self.book.active[address]=before

    def test_further_google_edit_before_export_acknowledgement_is_visible_and_can_be_resolved(self):
        self.google.cell(3,5)['effectiveValue']={'stringValue':'Pessoa Dois'}
        self.google.cell(3,12)['effectiveValue']={'stringValue':'Pessoa Um'}
        with patch('schedule.sheets_reconciliation.authorized_session',return_value=self.google):
            result=reconcile_confirmed_writes(self.admin)
        self.assertEqual(result.conflicts,1)
        self.book.active['F4']='Pessoa Um';self.book.active['M4']='Pessoa Dois'
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.assertEqual(self.owners(),[self.a.pk,self.b.pk])
        for shift in Shift.objects.filter(pk__in=self.ids):
            self.assertTrue(shift.requires_reconciliation)
            self.assertTrue(shift.source_snapshot['google_revert_export_pending'])
            self.assertTrue(shift.source_snapshot['google_revert_check_conflict'])
        self.google.cell(3,5)['effectiveValue']={'stringValue':'Pessoa Um'}
        self.google.cell(3,12)['effectiveValue']={'stringValue':'Pessoa Dois'}
        with patch('schedule.sheets_reconciliation.authorized_session',return_value=self.google):
            result=reconcile_confirmed_writes(self.admin)
        self.assertEqual(result.conflicts,0)
        sync_workbook(self.book,self.source.spreadsheet_id)
        for shift in Shift.objects.filter(pk__in=self.ids):
            self.assertFalse(shift.requires_reconciliation)
            self.assertNotIn('google_revert_export_pending',shift.source_snapshot)
            self.assertNotIn('google_revert_check_conflict',shift.source_snapshot)
