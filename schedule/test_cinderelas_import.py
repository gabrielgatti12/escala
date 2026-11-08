from datetime import datetime, timedelta
from io import BytesIO, StringIO
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook

from .cinderelas_import import sync_cinderelas
from .models import Exchange, Notification, ResidentAccess, SheetSource, Shift
from .services import propose_exchange, respond_exchange
from .sheets import load_export


class CinderelasImportTests(TestCase):
    def setUp(self):
        users=get_user_model()
        self.a=users.objects.create_user('cinderela-import-a', first_name='Pessoa Um')
        self.b=users.objects.create_user('cinderela-import-b', first_name='Pessoa Dois')
        for user in [self.a,self.b]:
            start=timezone.make_aware(datetime(2040,1,1,8))
            Shift.objects.create(external_id=f'roster-{user.pk}',source='weekly-roster',resident=user,starts_at=start,ends_at=start+timedelta(hours=4),sector='Ambulatório')
        self.source='cinderelas-import-test-source'
        SheetSource.objects.create(spreadsheet_id=self.source,format='cinderelas')

    def month(self,book,title,day):
        sheet=book.create_sheet(title)
        for col,label in zip(range(2,21,3),['SEGUNDA','TERÇA','QUARTA','QUINTA','SEXTA','SÁBADO','DOMINGO']):
            sheet.cell(2,col,label)
        col=2+3*day.weekday()
        sheet.cell(3,col,day)
        sheet.cell(4,1,'R1');sheet.cell(4,col,'18-24');sheet.cell(4,col+1,'Pessoa Um')
        sheet.cell(5,col,'18-24');sheet.cell(5,col+1,'Pessoa Dois')
        return sheet,col

    def book(self):
        book=Workbook();book.remove(book.active)
        sheet,col=self.month(book,'JANEIRO2035',datetime(2035,1,1))
        return book,sheet,col

    def test_months_coexist_repeat_without_duplicates_and_link_to_correct_tab(self):
        book,sheet,col=self.book();self.month(book,'FEVEREIRO2035',datetime(2035,2,1))
        original=self.a.username
        sync_cinderelas(book,self.source);ids=list(Shift.objects.filter(source=self.source).values_list('pk',flat=True))
        sync_cinderelas(book,self.source)
        shifts=Shift.objects.filter(source=self.source)
        self.assertEqual(shifts.count(),4)
        self.assertEqual(list(shifts.values_list('pk',flat=True)),ids)
        self.assertIn('JANEIRO2035',shifts.first().source_url)
        self.assertTrue(all((timezone.localtime(s.starts_at).hour,timezone.localtime(s.ends_at).hour)==(18,0) for s in shifts))
        self.assertEqual(get_user_model().objects.count(),2)
        self.a.refresh_from_db();self.assertEqual(self.a.username,original)
        self.assertFalse(Notification.objects.exists())

    def test_external_grade_change_updates_same_slot_and_notifies_affected_people(self):
        book,sheet,col=self.book();sync_cinderelas(book,self.source)
        first=Shift.objects.get(source=self.source,resident=self.a)
        sheet.cell(4,col+1,'Pessoa Dois');sync_cinderelas(book,self.source)
        first.refresh_from_db();self.assertEqual(first.resident_id,self.b.pk)
        self.assertEqual(Shift.objects.filter(source=self.source).count(),2)
        self.assertEqual(set(Notification.objects.values_list('resident_id',flat=True)),{self.a.pk,self.b.pk})

    def test_same_name_in_two_slots_is_two_independent_shifts(self):
        book,sheet,col=self.book();sheet.cell(5,col+1,'Pessoa Um')
        sync_cinderelas(book,self.source)
        self.assertEqual(Shift.objects.filter(source=self.source,resident=self.a).count(),2)

    def test_unknown_name_preserves_only_its_month_and_never_creates_identity(self):
        book,sheet,col=self.book();second,second_col=self.month(book,'FEVEREIRO2035',datetime(2035,2,1))
        sync_cinderelas(book,self.source)
        sheet.cell(4,col+1,'Nome Não Cadastrado')
        second.cell(4,second_col+1,'Pessoa Dois')
        summary=sync_cinderelas(book,self.source)
        self.assertIn('1 meses preservados',summary)
        held=Shift.objects.filter(source=self.source,source_tab=sheet.title)
        self.assertTrue(all(s.active and s.requires_reconciliation for s in held))
        self.assertEqual(get_user_model().objects.count(),2)
        self.assertEqual(Shift.objects.filter(source=self.source,source_tab=second.title,resident=self.b).count(),2)

    def test_removing_an_empty_month_or_bad_export_never_erases_other_months(self):
        book,sheet,col=self.book();second,second_col=self.month(book,'FEVEREIRO2035',datetime(2035,2,1))
        sync_cinderelas(book,self.source);book.remove(sheet)
        sync_cinderelas(book,self.source)
        self.assertEqual(Shift.objects.filter(source=self.source,active=True).count(),4)
        self.assertTrue(Shift.objects.filter(source=self.source,source_tab=sheet.title,requires_reconciliation=True).exists())

    def test_accepted_exchange_survives_old_source_and_acks_matching_form_update(self):
        book,sheet,col=self.book()
        sheet.cell(5,col+1).value=None
        next_col=col+3;sheet.cell(3,next_col,datetime(2035,1,2));sheet.cell(4,next_col,'18-24');sheet.cell(4,next_col+1,'Pessoa Dois')
        sync_cinderelas(book,self.source)
        first=Shift.objects.get(source=self.source,resident=self.a);second=Shift.objects.get(source=self.source,resident=self.b)
        exchange=propose_exchange(self.a,first.pk,second.pk);respond_exchange(self.b,exchange.pk,'accept')
        sync_cinderelas(book,self.source);first.refresh_from_db();second.refresh_from_db()
        self.assertEqual((first.resident_id,second.resident_id),(self.b.pk,self.a.pk))
        self.assertFalse(first.requires_reconciliation)
        sheet.cell(4,col+1,'Pessoa Dois');sheet.cell(4,next_col+1,'Pessoa Um')
        sync_cinderelas(book,self.source);first.refresh_from_db()
        self.assertFalse(first.requires_reconciliation)
        self.assertEqual(first.source_snapshot['resident_id'],self.b.pk)

    def test_changed_unaccepted_slot_cancels_obsolete_proposal(self):
        book,sheet,col=self.book();sheet.cell(5,col+1).value=None
        sheet.cell(3,col+3,datetime(2035,1,2));sheet.cell(4,col+3,'18-24');sheet.cell(4,col+4,'Pessoa Dois')
        sync_cinderelas(book,self.source)
        first=Shift.objects.get(source=self.source,resident=self.a);second=Shift.objects.get(source=self.source,resident=self.b)
        proposal=propose_exchange(self.a,first.pk,second.pk)
        sheet.cell(4,col+1,'Pessoa Dois');sync_cinderelas(book,self.source)
        proposal.refresh_from_db();self.assertEqual(proposal.status,'cancelled')

    def test_third_party_conflict_after_accepted_swap_stays_flagged_on_repeated_reads(self):
        user=get_user_model().objects.create_user('cinderela-import-c',first_name='Pessoa Três')
        start=timezone.make_aware(datetime(2040,1,1,8))
        Shift.objects.create(external_id='roster-third',source='weekly-roster',resident=user,starts_at=start,ends_at=start+timedelta(hours=4),sector='Outro')
        book,sheet,col=self.book();sheet.cell(5,col+1).value=None
        sheet.cell(3,col+3,datetime(2035,1,2));sheet.cell(4,col+3,'18-24');sheet.cell(4,col+4,'Pessoa Dois')
        sync_cinderelas(book,self.source)
        first=Shift.objects.get(source=self.source,resident=self.a);second=Shift.objects.get(source=self.source,resident=self.b)
        proposal=propose_exchange(self.a,first.pk,second.pk);respond_exchange(self.b,proposal.pk,'accept')
        sheet.cell(4,col+1,'Pessoa Três')
        for _ in range(2):
            sync_cinderelas(book,self.source);first.refresh_from_db()
            self.assertTrue(first.requires_reconciliation)
            self.assertEqual(first.resident_id,self.b.pk)
        sheet.cell(4,col+1).value=None
        sync_cinderelas(book,self.source);first.refresh_from_db()
        self.assertTrue(first.active);self.assertTrue(first.requires_reconciliation)
        self.assertEqual(first.resident_id,self.b.pk)

    def test_loader_marks_uncached_name_formulas_for_safe_parser_handling(self):
        book,sheet,col=self.book();sheet.cell(4,col+1,'="Pessoa Um"')
        stream=BytesIO();book.save(stream)
        loaded=load_export(stream.getvalue(),inspect_formulas=True)
        try:self.assertIn(sheet.cell(4,col+1).coordinate,loaded._uncached_formula_cells[sheet.title])
        finally:loaded.close()

    def test_renamed_month_keeps_primary_keys_and_removes_only_vacated_slots(self):
        book,sheet,col=self.book();sync_cinderelas(book,self.source)
        initial=set(Shift.objects.filter(source=self.source).values_list('pk',flat=True))
        sheet.title='JANEIRO 2035';sync_cinderelas(book,self.source)
        self.assertEqual(set(Shift.objects.filter(source=self.source,active=True).values_list('pk',flat=True)),initial)
        self.assertFalse(Shift.objects.filter(source=self.source,requires_reconciliation=True).exists())
        sheet.cell(5,col+1).value=None;sync_cinderelas(book,self.source)
        self.assertEqual(Shift.objects.filter(source=self.source,active=True).count(),1)

    def test_all_invalid_months_hold_known_entries_flagged_without_advancing_success(self):
        from .models import SyncState
        book,sheet,col=self.book();sync_cinderelas(book,self.source)
        last=SyncState.objects.get(source=self.source).last_success
        sheet.cell(4,col+1,'Nome Não Cadastrado')
        sync_cinderelas(book,self.source)
        self.assertEqual(SyncState.objects.get(source=self.source).last_success,last)
        self.assertEqual(Shift.objects.filter(source=self.source,active=True,requires_reconciliation=True).count(),2)
        self.assertTrue(Notification.objects.filter(resident=self.a,title='Há uma divergência na sua escala').exists())

    def test_worker_uses_cinderelas_parser_and_regular_week_label_is_not_required(self):
        book,_,_=self.book()
        with patch('schedule.management.commands.sync_sheets.fetch_workbook',return_value=book) as fetch:
            call_command('sync_sheets',self.source,only=True,format='cinderelas',stdout=StringIO())
        fetch.assert_called_once_with(self.source,inspect_formulas=True)
        self.assertEqual(Shift.objects.filter(source=self.source).count(),2)
        self.assertEqual(SheetSource.objects.get(spreadsheet_id=self.source).name,'Cinderelas 18–24')

    def test_admin_registers_format_and_cannot_switch_an_imported_source(self):
        self.a.is_staff=self.a.is_superuser=True;self.a.save();self.client.force_login(self.a)
        self.client.post('/fontes/adicionar/',{'url':self.source,'format':'cinderelas','name':'Cinderelas'})
        book,_,_=self.book();sync_cinderelas(book,self.source)
        response=self.client.post('/fontes/adicionar/',{'url':self.source,'format':'weekly'},follow=True)
        self.assertContains(response,'outro formato')
        self.assertEqual(SheetSource.objects.get(spreadsheet_id=self.source).format,'cinderelas')
