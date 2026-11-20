import copy
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from .bundle_exchanges import review_bundle, propose_bundle, respond_bundle
from .google_editor import EditorError
from .models import BundleExchange, BundleExchangeItem, Exchange, SheetsEditorConnection, SheetsWritePilot, SheetsWritePreview, SheetSource, Shift
from .sheets import sync_workbook, resident_for
from .sheets_write import BLUE_COLOR, FONT_COLOR_FIELD, preview_bundle_exchange, preview_exchange, apply_preview
from .test_sheets_write import GoogleFixture


class BundleSheetsPilotTests(TestCase):
    def setUp(self):
        self.clock=patch('django.utils.timezone.now',return_value=timezone.make_aware(datetime(2026,11,20,10)))
        self.clock.start();self.addCleanup(self.clock.stop)
        self.admin=get_user_model().objects.create_superuser('bundle-pilot-admin')
        self.other_admin=get_user_model().objects.create_superuser('bundle-pilot-other-admin')
        self.source=SheetSource.objects.create(spreadsheet_id='bundle-pilot-sheet-1234567890',name='Semana 23–29/11/2026',format='weekly')
        self.book=Workbook();sheet=self.book.active;sheet.title='Espelho Semanal'
        for address,value in {'B1':datetime(2026,11,25),'B2':'Quarta','A3':'INSTITUTO','B3':'PR Projeto Retina',
                              'C4':'7:45-12','F4':'Pessoa Um','A5':'HOSPITAL','B5':'CC Leonardo',
                              'C6':'13-19','F6':'Pessoa Um','A7':'INSTITUTO','B7':'Pré-Operatório',
                              'C8':'7:40-17','F8':'Pessoa Dois'}.items():sheet[address]=value
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.a=resident_for('Pessoa Um');self.b=resident_for('Pessoa Dois')
        self.offered=list(Shift.objects.filter(resident=self.a));self.requested=list(Shift.objects.filter(resident=self.b))
        report=review_bundle(self.a,self.b,[s.pk for s in self.offered],[s.pk for s in self.requested],'day')
        self.bundle=propose_bundle(self.a,self.b,[s.pk for s in self.offered],[s.pk for s in self.requested],'day',review_fingerprint=report['fingerprint'],acknowledged=True)
        self.pilot=SheetsWritePilot.objects.create(source=self.source,enabled=True,configured_by=self.admin)
        self.connection=SheetsEditorConnection.objects.create(email='bundle-editor@example.test',encrypted_refresh_token='fixture-not-real-token',connected_by=self.admin)
        self.google=GoogleFixture(self.book)
        self.session=patch('schedule.sheets_write.authorized_session',return_value=self.google)
        self.session.start();self.addCleanup(self.session.stop)
        self.client.force_login(self.admin)

    def accept(self):
        self.bundle=respond_bundle(self.b,self.bundle.pk,'accept',review_fingerprint=self.bundle.fingerprint,acknowledged=True)

    def preview(self):return preview_bundle_exchange(self.admin,self.bundle.pk)

    def test_two_for_one_preview_requires_both_acceptances_and_never_writes(self):
        with self.assertRaisesMessage(EditorError,'aceita pelos dois'):self.preview()
        self.assertEqual(self.google.calls,[])
        self.accept();result=self.preview()
        self.assertEqual(result.bundle_id,self.bundle.pk);self.assertIsNone(result.exchange_id)
        self.assertEqual(len(result.plan['summary']),3)
        self.assertEqual([cell['address'] for cell in result.plan['cells']],['F4','F6','F8'])
        self.assertEqual([cell['new_name'] for cell in result.plan['cells']],['Pessoa Dois','Pessoa Dois','Pessoa Um'])
        self.assertTrue(all(call[0]=='GET' for call in self.google.calls))
        self.assertIn('formulaValue',self.google.cell(3,5)['userEnteredValue'])

    def test_three_activities_written_together_with_only_name_and_font_color_fields(self):
        self.accept();result=apply_preview(self.admin,self.preview().pk)
        self.assertEqual(result.state,'done')
        posts=[call for call in self.google.calls if call[0]=='POST']
        self.assertEqual(len(posts),1)
        changes=posts[0][2]['json']['requests'];self.assertEqual(len(changes),3)
        self.assertTrue(all(change['updateCells']['fields']=='userEnteredValue,'+FONT_COLOR_FIELD for change in changes))
        self.assertEqual([self.google.cell(row,5)['effectiveValue']['stringValue'] for row in [3,5,7]],['Pessoa Dois','Pessoa Dois','Pessoa Um'])
        self.assertTrue(all(self.google.cell(row,5)['userEnteredFormat']['textFormat']['foregroundColorStyle']=={'rgbColor':BLUE_COLOR} for row in [3,5,7]))
        self.assertEqual(self.google.cell(3,2)['effectiveValue']['stringValue'],'7:45-12')
        self.assertEqual(self.google.cell(5,2)['effectiveValue']['stringValue'],'13-19')
        self.assertEqual(self.google.cell(7,2)['effectiveValue']['stringValue'],'7:40-17')
        with self.assertRaises(EditorError):apply_preview(self.admin,result.pk)

    def test_already_applied_bundle_is_verified_without_repeating_write(self):
        self.accept();apply_preview(self.admin,self.preview().pk)
        initial=len([call for call in self.google.calls if call[0]=='POST'])
        result=self.preview();self.assertTrue(result.plan['already_applied'])
        self.assertEqual(apply_preview(self.admin,result.pk).state,'done')
        self.assertEqual(len([call for call in self.google.calls if call[0]=='POST']),initial)

    def test_applied_bundle_can_correct_one_font_without_rewriting_any_names(self):
        self.accept();self.assertEqual(apply_preview(self.admin,self.preview().pk).state,'done')
        cell=self.google.cell(5,5)
        for kind in ['userEnteredFormat','effectiveFormat']:
            cell[kind]['textFormat']['foregroundColorStyle']={'rgbColor':{'red':0,'green':0,'blue':0}}
        original=[copy.deepcopy(self.google.cell(row,5)['userEnteredValue']) for row in [3,5,7]]
        preview=self.preview();self.assertTrue(preview.plan['already_applied']);self.assertTrue(preview.plan['needs_color'])
        self.assertEqual(apply_preview(self.admin,preview.pk).state,'done')
        update=[call for call in self.google.calls if call[0]=='POST'][-1][2]['json']['requests']
        self.assertEqual(len(update),1)
        self.assertEqual(update[0]['updateCells']['fields'],FONT_COLOR_FIELD)
        self.assertEqual(update[0]['updateCells']['range']['startRowIndex'],5)
        self.assertEqual([self.google.cell(row,5)['userEnteredValue'] for row in [3,5,7]],original)
        self.assertEqual(cell['userEnteredFormat']['textFormat']['foregroundColorStyle'],{'rgbColor':BLUE_COLOR})

    def test_partial_application_or_changed_source_never_writes_remaining_activities(self):
        self.accept();result=self.preview()
        self.google.cell(3,5)['effectiveValue']={'stringValue':'Pessoa Dois'}
        applied=apply_preview(self.admin,result.pk)
        self.assertEqual(applied.state,'conflict')
        self.assertFalse(any(call[0]=='POST' for call in self.google.calls))
        with self.assertRaisesMessage(EditorError,'parte do conjunto'):self.preview()

    def test_edited_terms_owner_metadata_or_acceptance_reject_before_google_read(self):
        self.accept()
        for change in ['time','owner','cell','ack','snapshot']:
            with self.subTest(change=change):
                self.bundle.refresh_from_db();self.offered[0].refresh_from_db()
                previous_snapshot=copy.deepcopy(self.bundle.snapshot)
                previous_ack=self.bundle.recipient_acknowledged_at
                original={field:getattr(self.offered[0],field) for field in ['ends_at','resident_id','source_cell']}
                if change=='time':Shift.objects.filter(pk=self.offered[0].pk).update(ends_at=self.offered[0].ends_at+timedelta(minutes=1))
                elif change=='owner':Shift.objects.filter(pk=self.offered[0].pk).update(resident=self.a)
                elif change=='cell':Shift.objects.filter(pk=self.offered[0].pk).update(source_cell='C10:G10')
                elif change=='ack':BundleExchange.objects.filter(pk=self.bundle.pk).update(recipient_acknowledged_at=None)
                else:
                    altered=copy.deepcopy(self.bundle.snapshot);altered['terms']['mode']='selected'
                    BundleExchange.objects.filter(pk=self.bundle.pk).update(snapshot=altered)
                self.google.calls.clear()
                with self.assertRaises(EditorError):self.preview()
                self.assertEqual(self.google.calls,[])
                Shift.objects.filter(pk=self.offered[0].pk).update(**original)
                BundleExchange.objects.filter(pk=self.bundle.pk).update(snapshot=previous_snapshot,recipient_acknowledged_at=previous_ack)

    def test_multiple_decisions_are_refused_for_both_legacy_and_bundle_pilot(self):
        self.accept()
        single=Exchange.objects.create(offered=self.offered[0],requested=self.requested[0],proposer=self.a,recipient=self.b,status='accepted')
        with self.assertRaisesMessage(EditorError,'mais de uma troca'):self.preview()
        with self.assertRaisesMessage(EditorError,'mais de uma troca'):preview_exchange(self.admin,single.pk)
        single.delete()
        duplicate=BundleExchange.objects.create(proposer=self.a,recipient=self.b,mode='selected',status='accepted',snapshot={},fingerprint='fixture',proposer_acknowledged_at=timezone.now())
        BundleExchangeItem.objects.create(exchange=duplicate,shift=self.offered[0],side='offered',original_owner=self.a)
        with self.assertRaisesMessage(EditorError,'mais de uma troca'):self.preview()

    def test_pilot_still_restricts_same_week_and_same_file(self):
        self.accept()
        for changes in [{'source':'other-file'},{'category':'night'},{'requires_reconciliation':True},
                        {'starts_at':timezone.make_aware(datetime(2026,11,30,8)),'ends_at':timezone.make_aware(datetime(2026,11,30,12))}]:
            with self.subTest(changes=changes):
                shift=self.offered[0];shift.refresh_from_db();previous={field:getattr(shift,field) for field in changes}
                Shift.objects.filter(pk=shift.pk).update(**changes)
                with self.assertRaises(EditorError):self.preview()
                Shift.objects.filter(pk=shift.pk).update(**previous)
        self.assertEqual(self.google.calls,[])

    def test_preview_decision_constraint_rejects_neither_or_both_targets(self):
        for targets in [{},{'exchange':Exchange.objects.create(offered=self.offered[0],requested=self.requested[0],proposer=self.a,recipient=self.b),'bundle':self.bundle}]:
            with self.subTest(targets=targets),self.assertRaises(IntegrityError),transaction.atomic():
                SheetsWritePreview.objects.create(account=self.admin,source=self.source,**targets)

    def test_new_admin_route_requires_admin_csrf_and_shows_all_rows(self):
        self.accept();url=reverse('sheets_write_bundle_preview',args=[self.bundle.pk])
        page=self.client.get(reverse('sheets_write_pilot'))
        self.assertContains(page,'Preparar prévia do conjunto')
        self.assertIn('no-store',page['Cache-Control'])
        strict=Client(enforce_csrf_checks=True);strict.force_login(self.admin)
        self.assertEqual(strict.post(url).status_code,403)
        self.client.force_login(self.a)
        self.assertEqual(self.client.post(url).status_code,403)
        self.client.force_login(self.other_admin)
        response=self.client.post(url)
        self.assertRedirects(response,reverse('sheets_write_pilot'))
        self.assertFalse(SheetsWritePreview.objects.exists())
        self.client.force_login(self.admin)
        response=self.client.post(url)
        result=SheetsWritePreview.objects.get()
        self.assertRedirects(response,reverse('sheets_write_review',args=[result.pk]))
        self.assertContains(self.client.get(response.url),'F4, F6, F8')
        self.assertContains(self.client.get(response.url),'Conferi todos os nomes')

    def test_other_decision_write_does_not_block_bundle_and_own_lease_does(self):
        self.accept();result=self.preview()
        single=Exchange.objects.create(offered=self.offered[0],requested=self.requested[0],proposer=self.a,recipient=self.b)
        SheetsWritePreview.objects.create(exchange=single,account=self.admin,source=self.source,state='writing',started_at=timezone.now())
        another=BundleExchange.objects.create(proposer=self.a,recipient=self.b,mode='selected',status='pending',snapshot={},fingerprint='fixture',proposer_acknowledged_at=timezone.now())
        SheetsWritePreview.objects.create(bundle=another,account=self.admin,source=self.source,state='writing',started_at=timezone.now())
        self.assertEqual(apply_preview(self.admin,result.pk).state,'done')
        own=self.preview()
        SheetsWritePreview.objects.create(bundle=self.bundle,account=self.admin,source=self.source,state='writing',started_at=timezone.now())
        with self.assertRaisesMessage(EditorError,'em andamento'):
            apply_preview(self.admin,own.pk)

    def test_readback_after_write_preserves_all_shift_ids_and_accepted_bundle(self):
        self.accept();ids=set(Shift.objects.values_list('pk',flat=True))
        self.assertEqual(apply_preview(self.admin,self.preview().pk).state,'done')
        self.book.active['F4']='Pessoa Dois';self.book.active['F6']='Pessoa Dois';self.book.active['F8']='Pessoa Um'
        summary=sync_workbook(self.book,self.source.spreadsheet_id)
        self.assertIn('0 exigem conciliação',summary)
        self.assertEqual(set(Shift.objects.values_list('pk',flat=True)),ids)
        self.assertEqual(Shift.objects.filter(active=True).count(),3)
        self.bundle.refresh_from_db();self.assertEqual(self.bundle.status,'accepted')
