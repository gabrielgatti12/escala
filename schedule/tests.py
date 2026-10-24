from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone
from .models import Shift, Exchange
from .services import propose_exchange, respond_exchange

class WorkflowTests(TestCase):
    def setUp(self):
        self.a = get_user_model().objects.create_user('ana')
        self.b = get_user_model().objects.create_user('bia')
        self.c = get_user_model().objects.create_user('carla')
        self.start = (timezone.now()+timedelta(days=7)).replace(microsecond=0)
        self.s1 = Shift.objects.create(external_id='1',resident=self.a,starts_at=self.start,ends_at=self.start+timedelta(hours=6),sector='UTI')
        self.s2 = Shift.objects.create(external_id='2',resident=self.b,starts_at=self.start+timedelta(days=1),ends_at=self.start+timedelta(days=1,hours=6),sector='PS')
    def propose(self):
        return propose_exchange(self.a,self.s1.pk,self.s2.pk)
    def test_requires_login(self):
        self.assertEqual(self.client.get('/').status_code,302)
    def test_proposal_does_not_change_owners(self):
        self.assertEqual(self.propose().status,'pending')
        self.s1.refresh_from_db()
        self.assertEqual(self.s1.resident,self.a)
    def test_both_parties_required(self):
        exchange=self.propose()
        for user in [self.a,self.c]:
            with self.assertRaises(ValidationError): respond_exchange(user,exchange.pk,'accept')
        respond_exchange(self.b,exchange.pk,'accept')
        self.s1.refresh_from_db(); self.s2.refresh_from_db()
        self.assertEqual((self.s1.resident,self.s2.resident),(self.b,self.a))
        with self.assertRaises(ValidationError): respond_exchange(self.b,exchange.pk,'accept')
    def test_cannot_offer_someone_elses_shift(self):
        with self.assertRaises(ValidationError): propose_exchange(self.c,self.s1.pk,self.s2.pk)
    def test_reject_preserves_owners(self):
        exchange=self.propose()
        respond_exchange(self.b,exchange.pk,'reject')
        self.s1.refresh_from_db()
        self.assertEqual(self.s1.resident,self.a)
    def test_cancel(self):
        exchange=self.propose()
        self.assertEqual(respond_exchange(self.a,exchange.pk,'cancel').status,'cancelled')
    def test_conflict_detected_at_acceptance(self):
        exchange=self.propose()
        Shift.objects.create(external_id='3',resident=self.a,starts_at=self.s2.starts_at,ends_at=self.s2.ends_at,sector='PS')
        with self.assertRaises(ValidationError): respond_exchange(self.b,exchange.pk,'accept')
        self.s1.refresh_from_db()
        self.assertEqual(self.s1.resident,self.a)
    def test_third_party_cannot_access_exchange(self):
        exchange=self.propose()
        self.client.force_login(self.c)
        self.assertEqual(self.client.post(f'/trocas/{exchange.pk}/accept/').status_code,404)
    def test_dashboard_and_csrf(self):
        self.client.force_login(self.a)
        self.assertContains(self.client.get('/'),'Minha escala')
        from django.test import Client
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.b)
        self.assertEqual(client.post('/trocas/criar/',{}).status_code,403)
    def test_import_repeatable_preserves_exchange(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'schedule.csv'
            path.write_text(f'id,residente,inicio,fim,setor\n1,ana,{self.s1.starts_at.isoformat()},{self.s1.ends_at.isoformat()},UTI\n2,bia,{self.s2.starts_at.isoformat()},{self.s2.ends_at.isoformat()},PS\n')
            call_command('import_schedule',str(path))
            call_command('import_schedule',str(path))
            self.assertEqual(Shift.objects.count(),2)
            exchange=self.propose()
            respond_exchange(self.b,exchange.pk,'accept')
            call_command('import_schedule',str(path))
            self.s1.refresh_from_db()
            self.assertEqual(self.s1.resident,self.b)
    def test_invalid_import_rolls_back(self):
        from django.core.management.base import CommandError
        with TemporaryDirectory() as folder:
            path=Path(folder)/'bad.csv'
            path.write_text(f'id,residente,inicio,fim,setor\nnew,ana,{self.s1.starts_at.isoformat()},{self.s1.ends_at.isoformat()},UTI\nbad,unknown,invalid,invalid,PS\n')
            with self.assertRaises(CommandError): call_command('import_schedule',str(path))
            self.assertEqual(Shift.objects.count(),2)
    def test_interest_is_owner_only(self):
        self.client.force_login(self.b)
        self.assertEqual(self.client.post(f'/plantoes/{self.s1.pk}/interesse/').status_code,404)
        self.client.force_login(self.a)
        self.assertEqual(self.client.post(f'/plantoes/{self.s1.pk}/interesse/').status_code,302)
        self.s1.refresh_from_db()
        self.assertTrue(self.s1.wants_exchange)
    def test_sheet_sync_idempotent_and_removed_shift(self):
        from openpyxl import Workbook
        from .sheets import sync_workbook
        from .models import RotationActivity
        book=Workbook(); sheet=book.active;sheet.title='Espelho Semanal'
        day=self.start.replace(tzinfo=None)
        sheet.append([None,day]);sheet.append([None,'Segunda'])
        sheet.append(['INSTITUTO','Ambulatório'])
        sheet.append([None,'R1','8-18','R1-A',None,'Residente Teste'])
        rotation=book.create_sheet('R1')
        rotation.append(['R1',None,'Período',day,day+timedelta(days=7)])
        rotation.append(['Segunda','Terça','Quarta','Quinta','Sexta'])
        rotation.append([None]);rotation.append([None])
        rotation.append(['R1 - A','Residente Teste'])
        rotation.append(['AMB']*5);rotation.append(['CC']*5)
        sync_workbook(book,'test-source');sync_workbook(book,'test-source')
        self.assertEqual(Shift.objects.filter(source='test-source').count(),1)
        self.assertEqual(RotationActivity.objects.count(),0)
        imported=Shift.objects.get(source='test-source')
        self.assertFalse(imported.resident.has_usable_password())
        imported.wants_exchange=True;imported.save()
        sync_workbook(book,'test-source');imported.refresh_from_db()
        self.assertTrue(imported.wants_exchange)
        sheet.cell(row=4,column=6,value='Outro Residente')
        sync_workbook(book,'test-source');imported.refresh_from_db()
        self.assertFalse(imported.active)
    def test_past_and_inactive_cannot_be_exchanged(self):
        self.s1.active=False;self.s1.save()
        with self.assertRaises(ValidationError): self.propose()
    def test_acceptance_clears_interest(self):
        self.s1.wants_exchange=True;self.s1.save()
        respond_exchange(self.b,self.propose().pk,'accept')
        self.s1.refresh_from_db()
        self.assertFalse(self.s1.wants_exchange)
    def test_shared_names_are_alternatives_not_two_shifts(self):
        from openpyxl import Workbook
        from .sheets import sync_workbook
        from .models import SharedActivity
        book=Workbook();sheet=book.active;sheet.title='Espelho Semanal'
        sheet.append([None,self.start.replace(tzinfo=None)])
        sheet.append([None,'Segunda'])
        sheet.append(['INSTITUTO','Ambulatório'])
        sheet.append([None,'R1','8-18','C1A-H','C1B-A','Pessoa Um','Pessoa Dois'])
        sheet.append(['EXTERNO','Wet lab'])
        sheet.append([None,'R1','10-12','C1A-H','C1B-A','Pessoa Um','Pessoa Dois'])
        sync_workbook(book,'paired-source')
        self.assertEqual(Shift.objects.filter(source='paired-source',active=True).count(),0)
        self.assertEqual(SharedActivity.objects.filter(source='paired-source').count(),2)
        for activity in SharedActivity.objects.filter(source='paired-source'):
            self.assertEqual(activity.residents.count(),2)
        sync_workbook(book,'paired-source')
        self.assertEqual(SharedActivity.objects.filter(source='paired-source').count(),2)
    def allocation_pair(self):
        from .models import SharedActivity
        start=self.start+timedelta(days=10)
        mine=SharedActivity.objects.create(external_id='shared-one',source='test',starts_at=start,ends_at=start+timedelta(hours=6),sector='Ambulatório')
        theirs=SharedActivity.objects.create(external_id='shared-two',source='test',starts_at=start,ends_at=start+timedelta(hours=3),sector='Wet lab')
        mine.residents.set([self.a,self.b]);theirs.residents.set([self.a,self.b])
        return mine,theirs
    def test_allocation_updates_calendar_only_after_both_confirm(self):
        from .services import propose_allocation,respond_allocation
        mine,theirs=self.allocation_pair()
        allocation=propose_allocation(self.a,mine.pk,theirs.pk)
        self.assertEqual(Shift.objects.count(),2)
        with self.assertRaises(ValidationError): respond_allocation(self.a,allocation.pk,'accept')
        respond_allocation(self.b,allocation.pk,'accept')
        self.assertEqual(Shift.objects.count(),4)
        mine.refresh_from_db();theirs.refresh_from_db()
        self.assertEqual((mine.allocated_to_id,theirs.allocated_to_id),(self.a.pk,self.b.pk))
        self.client.force_login(self.a)
        self.assertContains(self.client.get('/calendario/',{'month':mine.starts_at.strftime('%Y-%m')}),'Ambulatório')
        ics=self.client.get('/calendario.ics').content.decode()
        self.assertIn('SUMMARY:Ambulatório',ics)
        self.assertNotIn('SUMMARY:Wet lab',ics)
        with self.assertRaises(ValidationError): respond_allocation(self.b,allocation.pk,'accept')
    def test_allocation_cannot_be_proposed_by_third_party(self):
        from .services import propose_allocation
        mine,theirs=self.allocation_pair()
        with self.assertRaises(ValidationError): propose_allocation(self.c,mine.pk,theirs.pk)
    def test_rejected_allocation_creates_no_calendar_entries(self):
        from .services import propose_allocation,respond_allocation
        mine,theirs=self.allocation_pair()
        allocation=propose_allocation(self.a,mine.pk,theirs.pk)
        respond_allocation(self.b,allocation.pk,'reject')
        self.assertEqual(Shift.objects.count(),2)
    def test_allocation_conflict_rolls_back_both_assignments(self):
        from .services import propose_allocation,respond_allocation
        mine,theirs=self.allocation_pair()
        allocation=propose_allocation(self.a,mine.pk,theirs.pk)
        Shift.objects.create(external_id='conflict',resident=self.b,starts_at=theirs.starts_at,ends_at=theirs.ends_at,sector='Outro')
        with self.assertRaises(ValidationError): respond_allocation(self.b,allocation.pk,'accept')
        mine.refresh_from_db();theirs.refresh_from_db()
        self.assertIsNone(mine.allocated_to_id);self.assertIsNone(theirs.allocated_to_id)
        self.assertEqual(Shift.objects.count(),3)
    def test_allocation_api_protects_third_party(self):
        from .services import propose_allocation
        mine,theirs=self.allocation_pair()
        allocation=propose_allocation(self.a,mine.pk,theirs.pk)
        self.client.force_login(self.c)
        self.assertEqual(self.client.post(f'/divisoes/{allocation.pk}/accept/').status_code,404)
    def test_sheets_preserve_mutually_confirmed_allocation(self):
        from openpyxl import Workbook
        from .sheets import sync_workbook
        from .models import SharedActivity
        from .services import propose_allocation,respond_allocation
        book=Workbook();sheet=book.active;sheet.title='Espelho Semanal'
        sheet.append([None,(self.start+timedelta(days=20)).replace(tzinfo=None)])
        sheet.append([None,'Segunda']);sheet.append(['INSTITUTO','Ambulatório'])
        sheet.append([None,'R1','8-18','X','Y','Pessoa Um','Pessoa Dois'])
        sheet.append(['EXTERNO','Wet lab'])
        sheet.append([None,'R1','10-12','X','Y','Pessoa Um','Pessoa Dois'])
        sync_workbook(book,'confirm-source')
        mine,theirs=list(SharedActivity.objects.filter(source='confirm-source'))
        a,b=list(mine.residents.all())
        proposal=propose_allocation(a,mine.pk,theirs.pk)
        respond_allocation(b,proposal.pk,'accept')
        sync_workbook(book,'confirm-source')
        mine.refresh_from_db();theirs.refresh_from_db()
        self.assertEqual((mine.allocated_to_id,theirs.allocated_to_id),(a.pk,b.pk))
        self.assertEqual(Shift.objects.filter(external_id__startswith='allocation:',active=True).count(),2)
    def test_linked_administrator_uses_resident_identity(self):
        from .models import ResidentAccess
        admin=get_user_model().objects.create_superuser('coordenador',password='test-only-password')
        self.client.force_login(admin)
        response=self.client.post('/conta/vincular/',{'resident':self.a.pk})
        self.assertEqual(response.status_code,302)
        self.assertEqual(ResidentAccess.objects.get(account=admin).resident,self.a)
        response=self.client.get('/')
        self.assertTrue(response.context['has_offered'])
        self.assertEqual(response.context['resident_user'],self.a)
        self.client.post('/trocas/criar/',{'offered':self.s1.pk,'requested':self.s2.pk})
        self.assertEqual(Exchange.objects.get().proposer,self.a)
        ics=self.client.get('/calendario.ics').content.decode()
        self.assertIn(f'UID:shift-{self.s1.pk}@escala',ics)
        self.assertNotIn(f'UID:shift-{self.s2.pk}@escala',ics)
    def test_resident_cannot_link_to_another_identity(self):
        from .models import ResidentAccess
        self.client.force_login(self.a)
        self.assertEqual(self.client.post('/conta/vincular/',{'resident':self.b.pk}).status_code,403)
        self.assertFalse(ResidentAccess.objects.exists())
    def test_no_future_shifts_explains_why_exchange_is_disabled(self):
        Shift.objects.update(starts_at=timezone.now()-timedelta(days=2),ends_at=timezone.now()-timedelta(days=1))
        self.client.force_login(self.a)
        response=self.client.get('/')
        self.assertContains(response,'Não há plantões futuros importados')
        self.assertContains(response,'disabled')
        self.assertNotContains(response,'<h2>Referências de rotação</h2>')
    def test_headers_in_time_column_and_missing_times(self):
        from openpyxl import Workbook
        from .sheets import sync_workbook
        from .models import IncompleteActivity
        book=Workbook();sheet=book.active;sheet.title='Espelho Semanal'
        sheet.append([None,self.start.replace(tzinfo=None)])
        sheet.append([None,'Segunda'])
        sheet.append(['INSTITUTO','Ambulatório'])
        sheet.append([None,'R1','8-18','A',None,'Pessoa Um'])
        sheet.append(['UNEX','R2','U.E. Centro Norte'])
        sheet.append([None,None,'9-18','B',None,'Pessoa Dois'])
        sheet.append([None,None,None,'C',None,'Pessoa Tres'])
        sync_workbook(book,'header-source')
        shift=Shift.objects.get(source='header-source',source_cell='C6:G6')
        self.assertEqual(shift.sector,'UNEX · U.E. Centro Norte')
        self.assertEqual(timezone.localtime(shift.starts_at).hour,9)
        self.assertIn('09:00',str(shift))
        self.assertEqual(IncompleteActivity.objects.get(source='header-source').source_cell,'C7:G7')
        self.assertEqual(Shift.objects.filter(source='header-source').count(),2)
    def test_linked_recipient_can_confirm_exchange(self):
        from .models import ResidentAccess
        exchange=self.propose()
        account=get_user_model().objects.create_superuser('admin-recipient',password='test-only-password')
        ResidentAccess.objects.create(account=account,resident=self.b)
        self.client.force_login(account)
        self.assertContains(self.client.get('/'),f'/trocas/{exchange.pk}/accept/')
        self.client.post(f'/trocas/{exchange.pk}/accept/')
        exchange.refresh_from_db()
        self.assertEqual(exchange.status,'accepted')
    def test_drive_discovers_month_subfolders_and_all_pages(self):
        from unittest.mock import Mock
        from .drive import discover_folder,FOLDER_MIME,SHEET_MIME
        from .models import DriveFolder,SheetSource
        folder=DriveFolder.objects.create(folder_id='rootfolder0001',name='Escalas')
        SheetSource.objects.create(spreadsheet_id='spreadsheet002',enabled=False)
        payloads=[
            {'files':[{'id':'monthfolder001','name':'Outubro','mimeType':FOLDER_MIME}],'nextPageToken':'page-two'},
            {'files':[{'id':'spreadsheet001','name':'Semana 1','mimeType':SHEET_MIME}]},
            {'files':[{'id':'spreadsheet002','name':'Semana 2','mimeType':SHEET_MIME}]},
        ]
        session=Mock()
        responses=[]
        for payload in payloads:
            response=Mock();response.json.return_value=payload;responses.append(response)
        session.get.side_effect=responses
        self.assertEqual(discover_folder(session,folder),2)
        self.assertEqual(SheetSource.objects.count(),2)
        self.assertFalse(SheetSource.objects.get(spreadsheet_id='spreadsheet002').enabled)
        self.assertEqual(session.get.call_args_list[1].kwargs['params']['pageToken'],'page-two')
        self.assertIn('monthfolder001',session.get.call_args_list[2].kwargs['params']['q'])
    def test_failed_drive_discovery_does_not_save_partial_listing(self):
        from unittest.mock import Mock
        from .drive import discover_folder,FOLDER_MIME,SHEET_MIME
        from .models import DriveFolder,SheetSource
        folder=DriveFolder.objects.create(folder_id='rootfolder0001')
        response=Mock();response.json.return_value={'files':[{'id':'spreadsheet001','name':'Semana','mimeType':SHEET_MIME},{'id':'monthfolder001','name':'Mês','mimeType':FOLDER_MIME}]}
        session=Mock();session.get.side_effect=[response,RuntimeError('Unavailable')]
        with self.assertRaises(RuntimeError): discover_folder(session,folder)
        self.assertFalse(SheetSource.objects.exists())
        folder.refresh_from_db();self.assertIsNone(folder.last_success)
    def test_multiple_weekly_sources_are_unified_without_erasing_previous_week(self):
        from openpyxl import Workbook
        from .sheets import sync_workbook
        for offset,source in [(0,'week-source-one'),(7,'week-source-two')]:
            book=Workbook();sheet=book.active;sheet.title='Espelho Semanal'
            sheet.append([None,(self.start+timedelta(days=offset)).replace(tzinfo=None)])
            sheet.append([None,'Segunda']);sheet.append(['INSTITUTO','Ambulatório'])
            sheet.append([None,'R1','8-18','A',None,'Pessoa Um'])
            sync_workbook(book,source)
            if offset==7: sync_workbook(book,source)
        self.assertEqual(Shift.objects.filter(source__startswith='week-source',active=True).count(),2)
        self.assertEqual(Shift.objects.filter(source__startswith='week-source').values('resident_id').distinct().count(),1)
    def test_dashboard_opens_nearest_upcoming_week(self):
        self.client.force_login(self.a)
        response=self.client.get('/')
        day=timezone.localtime(self.s1.starts_at).date()
        self.assertEqual(response.context['week_start'],day-timedelta(days=day.weekday()))
        self.assertContains(response,'Solicitar troca')
    def test_week_selection_is_preserved_explicitly(self):
        self.client.force_login(self.a)
        response=self.client.get('/',{'week':'2026-09-28'})
        self.assertEqual(response.context['week_start'].isoformat(),'2026-09-28')
    def test_source_registration_is_administrator_only(self):
        self.client.force_login(self.a)
        self.assertEqual(self.client.get('/?tab=sources').status_code,403)
        self.assertEqual(self.client.post('/fontes/adicionar/',{'url':'https://docs.google.com/spreadsheets/d/'+'a'*30+'/edit'}).status_code,403)
    def test_admin_can_add_a_week_without_duplicates(self):
        from .models import SheetSource
        account=get_user_model().objects.create_superuser('admin-sources',password='test-only-password')
        self.client.force_login(account)
        url='https://docs.google.com/spreadsheets/d/'+'a'*30+'/edit?usp=sharing'
        for _ in range(2):
            self.assertEqual(self.client.post('/fontes/adicionar/',{'url':url,'name':'Próxima semana'}).status_code,302)
        self.assertEqual(SheetSource.objects.count(),1)
        self.assertContains(self.client.get('/?tab=sources'),'Próxima semana')
        self.assertContains(self.client.get('/?tab=sources'),'Aguardando importação')
    def test_source_registration_rejects_other_hosts(self):
        from .models import SheetSource
        account=get_user_model().objects.create_superuser('admin-invalid-source',password='test-only-password')
        self.client.force_login(account)
        for url in ['https://example.com/spreadsheets/d/'+'a'*30+'/edit','http://docs.google.com/spreadsheets/d/'+'a'*30+'/edit','https://docs.google.com.evil.example/spreadsheets/d/'+'a'*30+'/edit']:
            self.client.post('/fontes/adicionar/',{'url':url})
        self.assertEqual(SheetSource.objects.count(),0)
    def test_application_assets_are_registered(self):
        from django.contrib.staticfiles import finders
        self.assertTrue(finders.find('schedule/app.css'))
        self.assertTrue(finders.find('schedule/app.js'))
    def test_sync_labels_an_added_week_from_its_actual_dates(self):
        from openpyxl import Workbook
        from unittest.mock import patch
        from .models import SheetSource
        book=Workbook();sheet=book.active;sheet.title='Espelho Semanal'
        from datetime import datetime
        sheet.append([None,datetime(2026,10,26)])
        sheet.append([None,'Segunda']);sheet.append(['INSTITUTO','Ambulatório'])
        sheet.append([None,'R1','8-18','A',None,'Pessoa Um'])
        with patch('schedule.management.commands.sync_sheets.fetch_workbook',return_value=book):
            call_command('sync_sheets','a'*30)
        self.assertEqual(SheetSource.objects.get().name,'Semana de 26/10 a 01/11/2026')
