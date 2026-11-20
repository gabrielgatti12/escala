import copy
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from . import test_sheets_write as pilot_tests
from . import test_bundle_sheets_write as bundle_tests
from .google_editor import EditorError
from .models import (
    BundleExchange, BundleExchangeItem, Exchange, Notification, SheetsEditorConnection,
    SheetsSourceCheck, SheetsWritePreview, Shift, SyncHeartbeat,
)
from .sheets import sync_workbook
from .sheets_reconciliation import reconcile_confirmed_writes, reverted_identity_slots, source_conflict_shift_ids
from .sheets_write import apply_preview


class ConfirmedSingleReversalTests(TestCase):
    def setUp(self):
        pilot_tests.WeeklyWritePilotTests.setUp(self)
        self.reader = patch('schedule.sheets_reconciliation.authorized_session', return_value=self.google)
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def preview(self):
        return pilot_tests.WeeklyWritePilotTests.preview(self)

    def confirmed(self):
        result = apply_preview(self.admin, self.preview().pk)
        self.assertEqual(result.state, 'done')
        self.google.calls.clear()
        return result

    def original_names(self):
        for row, column, name in [(3, 5, 'Pessoa Um'), (3, 12, 'Pessoa Dois')]:
            self.google.cell(row, column).update(effectiveValue={'stringValue': name}, userEnteredValue={'stringValue': name})

    def owners(self):
        return dict(Shift.objects.values_list('pk', 'resident_id'))

    def test_complete_authenticated_undo_restores_both_event_ids_and_history_once(self):
        preview = self.confirmed()
        original = {self.offered.pk: self.a.pk, self.requested.pk: self.b.pk}
        previous = self.owners()
        self.exchange.refresh_from_db()
        accepted_at = self.exchange.resolved_at
        self.original_names()
        result = reconcile_confirmed_writes(self.admin)
        self.assertEqual((result.checked, result.reverted, result.conflicts), (1, 1, 0))
        self.assertEqual(self.owners(), original)
        self.exchange.refresh_from_db()
        preview.refresh_from_db()
        self.assertEqual(self.exchange.status, 'reverted')
        self.assertEqual(preview.state, 'reverted')
        self.assertEqual(self.exchange.resolved_at, accepted_at)
        self.assertIsNotNone(self.exchange.source_reverted_at)
        self.assertEqual(Notification.objects.filter(title='Troca desfeita na planilha').count(), 2)
        audit = SheetsSourceCheck.objects.get(status='reverted')
        self.assertEqual(set(audit.restored_shift_ids), set(original))
        self.assertEqual(len(audit.source_digest), 64)
        slots = reverted_identity_slots(self.source.spreadsheet_id)
        self.assertEqual({shift.pk for shift in slots}, set(original))
        for shift in slots:
            self.assertTrue(shift.source_snapshot['google_revert_export_pending'])
            self.assertEqual(shift.source_snapshot['google_revert_previous_owner'], previous[shift.pk])
        self.assertTrue(all(call[0] == 'GET' for call in self.google.calls))
        self.assertEqual(len(self.google.calls), 1)
        reconcile_confirmed_writes(self.admin)
        self.assertEqual(Notification.objects.filter(title='Troca desfeita na planilha').count(), 2)
        self.assertEqual(len(self.google.calls), 2)

    def test_second_source_edit_before_export_ack_is_flagged_until_authenticated_originals_return(self):
        self.confirmed()
        final_data = copy.deepcopy(self.google.data)
        self.original_names()
        self.assertEqual(reconcile_confirmed_writes(self.admin).reverted, 1)
        restored_owners = self.owners()
        self.google.data = final_data
        result = reconcile_confirmed_writes(self.admin)
        self.assertEqual((result.reverted, result.conflicts), (0, 1))
        self.assertEqual(self.owners(), restored_owners)
        for shift in Shift.objects.all():
            self.assertTrue(shift.source_snapshot['google_revert_check_conflict'])
            self.assertTrue(shift.source_snapshot['google_revert_export_pending'])
            self.assertTrue(shift.requires_reconciliation)
        # Even an export with original names cannot clear an authenticated conflict.
        sync_workbook(self.book, self.source.spreadsheet_id)
        for shift in Shift.objects.all():
            self.assertTrue(shift.source_snapshot['google_revert_check_conflict'])
            self.assertTrue(shift.source_snapshot['google_revert_export_pending'])
            self.assertTrue(shift.requires_reconciliation)
        with patch.object(self.google, 'request', side_effect=TimeoutError('fixture')):
            self.assertEqual(reconcile_confirmed_writes(self.admin).unavailable, 1)
        self.assertTrue(all(shift.source_snapshot['google_revert_check_conflict'] for shift in Shift.objects.all()))
        self.original_names()
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 0)
        self.assertFalse(Shift.objects.filter(requires_reconciliation=True).exists())
        for shift in Shift.objects.all():
            self.assertNotIn('google_revert_check_conflict', shift.source_snapshot)
            self.assertTrue(shift.source_snapshot['google_revert_export_pending'])
        sync_workbook(self.book, self.source.spreadsheet_id)
        self.assertEqual(self.owners(), restored_owners)
        self.assertTrue(all('google_revert_export_pending' not in shift.source_snapshot for shift in Shift.objects.all()))
        self.assertTrue(all(call[0] == 'GET' for call in self.google.calls))

    def test_edit_after_partial_export_ack_guards_every_slot_until_new_authenticated_resolution(self):
        self.confirmed()
        final_data = copy.deepcopy(self.google.data)
        self.original_names()
        self.assertEqual(reconcile_confirmed_writes(self.admin).reverted, 1)
        restored_owners = self.owners()
        self.book.active['M4'] = 'Pessoa Um'  # Only F4 acknowledges the restore.
        sync_workbook(self.book, self.source.spreadsheet_id)
        self.offered.refresh_from_db()
        self.requested.refresh_from_db()
        self.assertNotIn('google_revert_export_pending', self.offered.source_snapshot)
        self.assertTrue(self.requested.source_snapshot['google_revert_export_pending'])
        self.google.data = final_data
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 1)
        self.book.active['M4'] = 'Pessoa Dois'
        sync_workbook(self.book, self.source.spreadsheet_id)
        for shift in Shift.objects.all():
            self.assertTrue(shift.source_snapshot['google_revert_check_conflict'])
            self.assertTrue(shift.source_snapshot['google_revert_export_pending'])
            self.assertTrue(shift.requires_reconciliation)
        self.assertEqual(self.owners(), restored_owners)
        self.original_names()
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 0)
        sync_workbook(self.book, self.source.spreadsheet_id)
        self.assertFalse(Shift.objects.filter(requires_reconciliation=True).exists())
        self.assertTrue(all('google_revert_export_pending' not in shift.source_snapshot for shift in Shift.objects.all()))
        self.assertEqual(self.owners(), restored_owners)

    def test_cached_original_export_without_a_confirmed_write_cannot_undo_acceptance(self):
        previous = self.owners()
        result = reconcile_confirmed_writes(self.admin)
        self.assertEqual(result.checked, 0)
        self.assertEqual(self.owners(), previous)
        self.assertFalse(self.google.calls)
        self.assertFalse(SheetsSourceCheck.objects.exists())

    def test_google_still_has_all_swapped_names_preserves_accepted_decision(self):
        self.confirmed()
        previous = self.owners()
        result = reconcile_confirmed_writes(self.admin)
        self.assertEqual((result.checked, result.reverted), (1, 0))
        self.assertEqual(self.owners(), previous)
        self.assertEqual(SheetsSourceCheck.objects.get().status, 'current')
        self.exchange.refresh_from_db()
        self.assertEqual(self.exchange.status, 'accepted')
        self.assertFalse(Shift.objects.filter(requires_reconciliation=True).exists())

    def test_partial_undo_preserves_all_names_and_does_not_cancel_another_request(self):
        self.confirmed()
        previous = self.owners()
        pending = Exchange.objects.create(offered=self.offered, requested=self.requested, proposer=self.b, recipient=self.a)
        self.google.cell(3, 5)['effectiveValue'] = {'stringValue': 'Pessoa Um'}
        result = reconcile_confirmed_writes(self.admin)
        self.assertEqual((result.reverted, result.conflicts), (0, 1))
        self.assertEqual(self.owners(), previous)
        self.assertEqual(Shift.objects.filter(requires_reconciliation=True).count(), 2)
        self.assertEqual(SheetsSourceCheck.objects.get().status, 'conflict')
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'pending')
        self.assertFalse(Notification.objects.filter(title='Troca desfeita na planilha').exists())
        self.assertTrue(all(call[0] == 'GET' for call in self.google.calls))

    def test_authenticated_conflict_survives_cached_import_until_fresh_google_read_resolves_it(self):
        self.confirmed()
        final_data = copy.deepcopy(self.google.data)
        previous = self.owners()
        pending = BundleExchange.objects.create(proposer=self.b, recipient=self.a, mode='selected',
                                               snapshot={}, fingerprint='fixture', proposer_acknowledged_at=timezone.now())
        BundleExchangeItem.objects.create(exchange=pending, shift=self.offered, side='offered', original_owner=self.b)
        BundleExchangeItem.objects.create(exchange=pending, shift=self.requested, side='requested', original_owner=self.a)
        self.google.cell(3, 5)['effectiveValue'] = {'stringValue': 'Pessoa Um'}
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 1)
        expected_ids = {self.offered.pk, self.requested.pk}
        self.assertEqual(source_conflict_shift_ids(self.source.spreadsheet_id), expected_ids)
        summary = sync_workbook(self.book, self.source.spreadsheet_id)
        self.assertIn('2 exigem conciliação', summary)
        self.assertEqual(self.owners(), previous)
        self.assertEqual(set(Shift.objects.filter(requires_reconciliation=True).values_list('pk', flat=True)), expected_ids)
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'pending')
        # Transport failure cannot erase an earlier conclusive conflict audit.
        with patch.object(self.google, 'request', side_effect=TimeoutError('fixture')):
            self.assertEqual(reconcile_confirmed_writes(self.admin).unavailable, 1)
        self.assertEqual(source_conflict_shift_ids(self.source.spreadsheet_id), expected_ids)
        self.google.data = final_data
        result = reconcile_confirmed_writes(self.admin)
        self.assertEqual((result.checked, result.reverted, result.conflicts), (1, 0, 0))
        self.assertEqual(source_conflict_shift_ids(self.source.spreadsheet_id), set())
        self.assertFalse(Shift.objects.filter(requires_reconciliation=True).exists())
        self.assertEqual(self.owners(), previous)

    def test_confirmed_current_does_not_clear_unrelated_preexisting_reconciliation_flag(self):
        self.confirmed()
        Shift.objects.filter(pk=self.offered.pk).update(requires_reconciliation=True)
        self.assertEqual(reconcile_confirmed_writes(self.admin).checked, 1)
        self.assertTrue(Shift.objects.get(pk=self.offered.pk).requires_reconciliation)

    def test_third_name_changed_time_or_changed_cell_count_never_restore_one_side(self):
        self.confirmed()
        original_data = copy.deepcopy(self.google.data)
        previous = self.owners()
        for case in ['third_name', 'time', 'blank_name', 'duplicate_name', 'new_sheet']:
            with self.subTest(case=case):
                self.google.data = copy.deepcopy(original_data)
                self.original_names()
                if case == 'third_name':
                    self.google.cell(3, 5)['effectiveValue'] = {'stringValue': 'Terceira Pessoa'}
                elif case == 'time':
                    self.google.cell(3, 2)['effectiveValue'] = {'stringValue': '9-12'}
                elif case == 'blank_name':
                    self.google.cell(3, 5).clear()
                elif case == 'duplicate_name':
                    self.google.cell(3, 6).update(effectiveValue={'stringValue': 'Pessoa Um'})
                else:
                    self.google.data['sheets'][0]['properties']['sheetId'] = 99
                result = reconcile_confirmed_writes(self.admin)
                self.assertEqual(result.reverted, 0)
                self.assertEqual(result.conflicts, 1)
                self.assertEqual(self.owners(), previous)

    def test_auth_failure_and_timeout_preserve_local_state_without_leaking_error(self):
        self.confirmed()
        previous = self.owners()
        self.original_names()
        with patch('schedule.sheets_reconciliation.authorized_session', side_effect=EditorError('SECRET-FIXTURE-TOKEN')):
            result = reconcile_confirmed_writes(self.admin)
        self.assertEqual(result.unavailable, 1)
        with patch.object(self.google, 'request', side_effect=TimeoutError('SECRET-FIXTURE-TOKEN')):
            result = reconcile_confirmed_writes(self.admin)
        self.assertEqual(result.unavailable, 1)
        self.assertEqual(self.owners(), previous)
        self.assertFalse(Shift.objects.filter(requires_reconciliation=True).exists())
        self.assertFalse(SheetsSourceCheck.objects.filter(message__contains='SECRET').exists())

    def test_failed_google_read_records_safe_actionable_reason_without_exception_body(self):
        self.confirmed()
        from requests import HTTPError
        from types import SimpleNamespace
        previous=self.owners()
        for status,expected in [(400,'HTTP 400'),(401,'Reconecte'),(403,'HTTP 403'),(429,'limite')]:
            error=HTTPError('SECRET token-body https://private.invalid',response=SimpleNamespace(status_code=status))
            with self.subTest(status=status), patch.object(self.google,'request',side_effect=error):
                result=reconcile_confirmed_writes(self.admin)
            self.assertEqual(result.unavailable,1)
            audit=SheetsSourceCheck.objects.first()
            self.assertIn(expected,audit.message)
            self.assertNotIn('SECRET',audit.message)
            self.assertEqual(self.owners(),previous)

    def test_older_single_plan_without_bundle_fields_can_be_reconciled(self):
        preview = self.confirmed()
        plan = copy.deepcopy(preview.plan)
        plan.pop('decision_type')
        plan.pop('needs_color', None)
        for row in plan['app_state']:
            row.pop('side')
            row.pop('original_owner')
        SheetsWritePreview.objects.filter(pk=preview.pk).update(plan=plan)
        self.original_names()
        self.assertEqual(reconcile_confirmed_writes(self.admin).reverted, 1)

    def test_changed_display_names_cannot_make_final_source_names_look_like_original_owners(self):
        self.confirmed()
        previous = self.owners()
        self.a.first_name, self.a.last_name = 'Pessoa', 'Dois'
        self.b.first_name, self.b.last_name = 'Pessoa', 'Um'
        self.a.save(update_fields=['first_name', 'last_name'])
        self.b.save(update_fields=['first_name', 'last_name'])
        result = reconcile_confirmed_writes(self.admin)
        self.assertEqual((result.reverted, result.conflicts), (0, 1))
        self.assertEqual(self.owners(), previous)

    def test_later_accepted_decision_or_changed_local_metadata_blocks_restoration(self):
        self.confirmed()
        self.original_names()
        previous = self.owners()
        later = Exchange.objects.create(offered=self.offered, requested=self.requested, proposer=self.b, recipient=self.a, status='accepted')
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 1)
        self.assertEqual(self.owners(), previous)
        later.delete()
        Shift.objects.filter(pk=self.offered.pk).update(source_cell='C5:G5')
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 1)
        self.assertEqual(self.owners(), previous)

    def test_newer_accepted_bundle_blocks_undo_and_is_not_cancelled(self):
        self.confirmed()
        self.original_names()
        previous = self.owners()
        later = BundleExchange.objects.create(proposer=self.b, recipient=self.a, mode='selected', status='accepted',
                                             snapshot={}, fingerprint='fixture', proposer_acknowledged_at=timezone.now())
        BundleExchangeItem.objects.create(exchange=later, shift=self.offered, side='offered', original_owner=self.b)
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 1)
        self.assertEqual(self.owners(), previous)
        later.refresh_from_db()
        self.assertEqual(later.status, 'accepted')

    def test_other_administrator_and_resident_cannot_borrow_connection(self):
        self.confirmed()
        self.original_names()
        previous = self.owners()
        self.assertEqual(reconcile_confirmed_writes(self.other_admin).unavailable, 1)
        self.assertEqual(reconcile_confirmed_writes(self.a).unavailable, 0)
        self.assertEqual(self.owners(), previous)
        self.assertFalse(self.google.calls)

    def test_current_administrator_can_check_history_with_their_own_google_connection(self):
        self.confirmed()
        self.original_names()
        SheetsEditorConnection.objects.update(connected_by=self.other_admin)
        with patch('schedule.sheets_reconciliation.authorized_session',return_value=self.google) as session:
            result=reconcile_confirmed_writes(self.other_admin)
        session.assert_called_once_with(self.other_admin)
        self.assertEqual(result.reverted,1)
        self.exchange.refresh_from_db()
        self.assertEqual(self.exchange.status,'reverted')
        self.assertEqual(SheetsSourceCheck.objects.get().account_id,self.other_admin.pk)
        self.assertTrue(all(call[0]=='GET' for call in self.google.calls))

    def test_independent_check_works_during_queued_import_and_shows_reason(self):
        self.confirmed()
        self.google.cell(3,5)['effectiveValue']={'stringValue':'Pessoa Um'}
        heartbeat,_=SyncHeartbeat.objects.get_or_create()
        heartbeat.refresh_requested_at=timezone.now()
        heartbeat.save()
        requested_at=heartbeat.refresh_requested_at
        response=self.client.post(reverse('request_source_refresh'),{'check_only':'yes'},follow=True)
        self.assertContains(response,'A planilha desfez apenas parte da troca')
        self.assertContains(response,'Conferir trocas agora')
        heartbeat.refresh_from_db()
        self.assertEqual(heartbeat.refresh_requested_at,requested_at)
        self.exchange.refresh_from_db()
        self.assertEqual(self.exchange.status,'accepted')

    def test_only_latest_confirmed_preview_is_checked_once_per_decision(self):
        self.confirmed()
        latest = self.confirmed()
        self.original_names()
        self.assertEqual(reconcile_confirmed_writes(self.admin).reverted, 1)
        self.assertEqual(SheetsSourceCheck.objects.get().preview_id, latest.pk)
        self.assertEqual(set(SheetsWritePreview.objects.values_list('state', flat=True)), {'reverted'})
        self.assertEqual(len(self.google.calls), 1)

    def test_refresh_post_runs_authoritative_check_and_enqueues_regular_import(self):
        self.confirmed()
        self.original_names()
        response = self.client.post(reverse('request_source_refresh'))
        self.assertEqual(response.status_code, 302)
        self.exchange.refresh_from_db()
        self.assertEqual(self.exchange.status, 'reverted')
        self.assertIsNotNone(SyncHeartbeat.objects.get().refresh_requested_at)
        self.assertEqual([call[0] for call in self.google.calls], ['GET'])

    def test_refresh_get_resident_and_missing_csrf_do_not_read_google(self):
        self.confirmed()
        self.original_names()
        url = reverse('request_source_refresh')
        self.assertEqual(self.client.get(url).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        self.assertEqual(strict.post(url).status_code, 403)
        self.client.force_login(self.a)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertFalse(self.google.calls)

    def test_unavailable_oauth_still_queues_refresh_and_preserves_decision(self):
        self.confirmed()
        previous = self.owners()
        SheetsEditorConnection.objects.all().delete()
        response = self.client.post(reverse('request_source_refresh'))
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(SyncHeartbeat.objects.get().refresh_requested_at)
        self.assertEqual(self.owners(), previous)
        self.assertFalse(self.google.calls)


class ConfirmedBundleReversalTests(TestCase):
    def setUp(self):
        bundle_tests.BundleSheetsPilotTests.setUp(self)
        self.reader = patch('schedule.sheets_reconciliation.authorized_session', return_value=self.google)
        self.reader.start()
        self.addCleanup(self.reader.stop)
        bundle_tests.BundleSheetsPilotTests.accept(self)

    def preview(self):
        return bundle_tests.BundleSheetsPilotTests.preview(self)

    def confirmed(self):
        preview = apply_preview(self.admin, self.preview().pk)
        self.assertEqual(preview.state, 'done')
        self.google.calls.clear()
        return preview

    def original_names(self):
        for row, name in [(3, 'Pessoa Um'), (5, 'Pessoa Um'), (7, 'Pessoa Dois')]:
            self.google.cell(row, 5).update(effectiveValue={'stringValue': name}, userEnteredValue={'stringValue': name})

    def test_two_for_one_undo_restores_all_items_and_preserves_both_acceptance_timestamps(self):
        preview = self.confirmed()
        before = {shift.pk: self.a.pk for shift in self.offered} | {shift.pk: self.b.pk for shift in self.requested}
        self.bundle.refresh_from_db()
        timestamps = (self.bundle.resolved_at, self.bundle.proposer_acknowledged_at, self.bundle.recipient_acknowledged_at)
        self.original_names()
        self.assertEqual(reconcile_confirmed_writes(self.admin).reverted, 1)
        self.assertEqual(dict(Shift.objects.values_list('pk', 'resident_id')), before)
        self.bundle.refresh_from_db()
        preview.refresh_from_db()
        self.assertEqual((self.bundle.status, preview.state), ('reverted', 'reverted'))
        self.assertEqual((self.bundle.resolved_at, self.bundle.proposer_acknowledged_at, self.bundle.recipient_acknowledged_at), timestamps)
        self.assertEqual({shift.pk for shift in reverted_identity_slots(self.source.spreadsheet_id)}, set(before))
        self.assertEqual(Notification.objects.filter(title='Troca desfeita na planilha').count(), 2)
        self.assertEqual([call[0] for call in self.google.calls], ['GET'])

    def test_partial_two_for_one_undo_and_changed_consent_restore_nothing(self):
        self.confirmed()
        previous = dict(Shift.objects.values_list('pk', 'resident_id'))
        self.google.cell(3, 5)['effectiveValue'] = {'stringValue': 'Pessoa Um'}
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 1)
        self.assertEqual(dict(Shift.objects.values_list('pk', 'resident_id')), previous)
        self.original_names()
        BundleExchange.objects.filter(pk=self.bundle.pk).update(recipient_acknowledged_at=None)
        self.assertEqual(reconcile_confirmed_writes(self.admin).conflicts, 1)
        self.assertEqual(dict(Shift.objects.values_list('pk', 'resident_id')), previous)

    def test_restoring_bundle_cancels_only_pending_requests_involving_restored_items(self):
        self.confirmed()
        pending = Exchange.objects.create(offered=self.offered[0], requested=self.requested[0], proposer=self.b, recipient=self.a)
        self.original_names()
        self.assertEqual(reconcile_confirmed_writes(self.admin).reverted, 1)
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'cancelled')
