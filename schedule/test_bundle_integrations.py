from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook

from .bundle_exchanges import propose_bundle,respond_bundle,review_bundle
from .bundle_integrations import accepted_bundle_shift_ids,cancel_pending_bundles,expire_pending_bundles
from .live import data_revision
from .models import BundleExchange,Notification,Shift,SheetSource
from .reminders import refresh_request_notifications
from .sheets import resident_for,sync_workbook


class BundleSourceIntegrationTests(TestCase):
    def setUp(self):
        self.now=timezone.make_aware(datetime(2035,1,1,8))
        self.clock=patch('django.utils.timezone.now',return_value=self.now)
        self.clock.start();self.addCleanup(self.clock.stop)
        self.source='bundle-weekly-source-1234567890'
        self.book=Workbook();sheet=self.book.active;sheet.title='Espelho Semanal'
        sheet['B1']=datetime(2035,1,3);sheet['B2']='Quarta';sheet['A3']='INSTITUTO';sheet['B3']='PR'
        sheet['C4']='7:45-12';sheet['F4']='Pessoa A'
        sheet['B5']='CC Leonardo';sheet['C6']='13-19';sheet['F6']='Pessoa A'
        sheet['B7']='Pré-op';sheet['C8']='7:40-17';sheet['F8']='Pessoa B'
        sync_workbook(self.book,self.source)
        SheetSource.objects.create(spreadsheet_id=self.source,format='weekly')
        self.a=resident_for('Pessoa A');self.b=resident_for('Pessoa B')
        self.offered=list(Shift.objects.filter(resident=self.a).order_by('starts_at'))
        self.requested=list(Shift.objects.filter(resident=self.b))

    def propose(self):
        ids=[item.pk for item in self.offered];other=[item.pk for item in self.requested]
        report=review_bundle(self.a,self.b,ids,other,'day')
        return propose_bundle(self.a,self.b,ids,other,'day',review_fingerprint=report['fingerprint'],acknowledged=True)

    def accept(self,proposal):
        return respond_bundle(self.b,proposal.pk,'accept',review_fingerprint=proposal.fingerprint,acknowledged=True)

    def test_old_source_never_undoes_accepted_two_for_one_and_ack_keeps_uids(self):
        proposal=self.accept(self.propose())
        ids=set(Shift.objects.values_list('pk',flat=True))
        self.assertEqual(accepted_bundle_shift_ids(self.source),ids)
        sync_workbook(self.book,self.source)
        self.assertEqual(Shift.objects.filter(resident=self.a).count(),1)
        self.assertEqual(Shift.objects.filter(resident=self.b).count(),2)
        self.book.active['F4']='Pessoa B';self.book.active['F6']='Pessoa B';self.book.active['F8']='Pessoa A'
        for _ in range(2):
            summary=sync_workbook(self.book,self.source)
            self.assertEqual(set(Shift.objects.values_list('pk',flat=True)),ids)
            self.assertEqual(Shift.objects.filter(active=True).count(),3)
            self.assertIn('0 exigem conciliação',summary)
        # A cached old export after acknowledgement is still read-only evidence.
        self.book.active['F4']='Pessoa A';self.book.active['F6']='Pessoa A';self.book.active['F8']='Pessoa B'
        sync_workbook(self.book,self.source)
        self.assertEqual(set(Shift.objects.values_list('pk',flat=True)),ids)
        self.assertEqual(Shift.objects.filter(resident=self.a).count(),1)
        self.assertEqual(Shift.objects.filter(resident=self.b).count(),2)

    def test_removed_pending_slot_cancels_entire_bundle_and_closes_notice(self):
        proposal=self.propose()
        note=Notification.objects.get(payload__request_type='bundle',payload__request_id=proposal.pk)
        self.book.active['F6']=None
        sync_workbook(self.book,self.source)
        proposal.refresh_from_db();note.refresh_from_db()
        self.assertEqual(proposal.status,'cancelled')
        self.assertIsNotNone(note.read_at)
        self.assertEqual(note.payload['status'],'cancelled')
        self.assertEqual(Shift.objects.filter(resident=self.a,active=True).count(),1)
        self.assertEqual(Shift.objects.filter(resident=self.b,active=True).count(),1)

    def test_source_addition_that_changes_full_day_invalidates_pending_before_reminder(self):
        proposal=self.propose()
        self.book.active['B9']='Outro estágio';self.book.active['C10']='19-20';self.book.active['F10']='Pessoa A'
        sync_workbook(self.book,self.source)
        refresh_request_notifications(now=self.now+timedelta(hours=25))
        proposal.refresh_from_db()
        self.assertEqual(proposal.status,'cancelled')
        self.assertFalse(Notification.objects.filter(payload__request_type='bundle',read_at__isnull=True).exists())

    def test_notification_is_private_points_to_review_and_reminder_is_reused(self):
        proposal=self.propose()
        notice=Notification.objects.get(payload__request_type='bundle')
        self.assertEqual(notice.resident_id,self.b.pk)
        self.assertEqual(notice.link,f'/trocas/combinadas/{proposal.pk}/')
        self.assertIn('2 atividade(s) por 1 atividade(s)',notice.message)
        refresh_request_notifications(now=self.now+timedelta(hours=25))
        self.assertEqual(Notification.objects.filter(payload__request_type='bundle').count(),2)
        refresh_request_notifications(now=self.now+timedelta(hours=26))
        self.assertEqual(Notification.objects.filter(payload__request_type='bundle').count(),2)
        self.accept(proposal)
        self.assertFalse(Notification.objects.filter(payload__request_type='bundle',read_at__isnull=True).exists())

    def test_live_revision_changes_when_a_bundle_is_proposed_and_closed(self):
        before=data_revision(self.b)
        proposal=self.propose();created=data_revision(self.b)
        self.assertNotEqual(before,created)
        self.accept(proposal)
        self.assertNotEqual(created,data_revision(self.b))

    def test_expired_bundle_cancels_before_next_google_read(self):
        proposal=self.propose()
        self.assertEqual(expire_pending_bundles(now=self.now+timedelta(days=3)),1)
        proposal.refresh_from_db();self.assertEqual(proposal.status,'cancelled')
        self.assertFalse(Notification.objects.filter(payload__request_type='bundle',read_at__isnull=True).exists())

    def test_cancelling_one_pending_item_invalidates_whole_group_only_once(self):
        proposal=self.propose()
        self.assertEqual(cancel_pending_bundles([self.offered[0].pk]),1)
        self.assertEqual(cancel_pending_bundles([self.offered[1].pk]),0)
        proposal.refresh_from_db();self.assertEqual(proposal.status,'cancelled')

    def test_accepted_bundle_shift_removed_from_source_is_retained_for_reconciliation(self):
        self.accept(self.propose());ids=set(Shift.objects.values_list('pk',flat=True))
        self.book.active['F6']=None
        summary=sync_workbook(self.book,self.source)
        self.assertEqual(set(Shift.objects.filter(active=True).values_list('pk',flat=True)),ids)
        self.assertIn('1 exigem conciliação',summary)

    def test_local_reimport_does_not_restore_old_owner_of_accepted_bundle(self):
        # Local CSV sources keep stable external IDs; accepted bundles must be
        # protected even when a stale file still lists the previous owner.
        for item in self.offered+self.requested:
            item.source='';item.save(update_fields=['source'])
        self.accept(self.propose())
        with TemporaryDirectory() as directory:
            file=Path(directory)/'roster.csv'
            lines=['id,residente,inicio,fim,setor']
            for item,owner in [(s,self.a) for s in self.offered]+[(s,self.b) for s in self.requested]:
                lines.append(f'{item.external_id},{owner.username},{item.starts_at.isoformat()},{item.ends_at.isoformat()},{item.sector}')
            file.write_text('\n'.join(lines))
            call_command('import_schedule',str(file),stdout=StringIO())
        self.assertEqual(Shift.objects.filter(resident=self.a).count(),1)
        self.assertEqual(Shift.objects.filter(resident=self.b).count(),2)

    def test_third_party_source_owner_marks_conflict_without_duplicating_assignment(self):
        self.accept(self.propose())
        ids=set(Shift.objects.values_list('pk',flat=True))
        self.book.active['F4']='Terceira Pessoa'
        summary=sync_workbook(self.book,self.source)
        self.assertEqual(set(Shift.objects.values_list('pk',flat=True)),ids)
        self.offered[0].refresh_from_db()
        self.assertEqual(self.offered[0].resident_id,self.b.pk)
        self.assertTrue(self.offered[0].requires_reconciliation)
        self.assertIn('1 exigem conciliação',summary)
        self.book.active['F4']='Pessoa A'
        summary=sync_workbook(self.book,self.source)
        self.offered[0].refresh_from_db()
        self.assertFalse(self.offered[0].requires_reconciliation)
        self.assertIn('0 exigem conciliação',summary)
