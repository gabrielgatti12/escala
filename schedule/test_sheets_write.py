import copy
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.utils.datetime import to_excel

from .google_editor import EditorError, SCOPES, encrypt_token
from .models import SheetsEditorConnection, SheetsWritePilot, SheetsWritePreview, SheetSource, Shift
from .services import propose_exchange, respond_exchange
from .sheets import sync_workbook, resident_for
from .sheets_write import BLUE_COLOR, FONT_COLOR_FIELD, preview_exchange, apply_preview


class Reply:
    def __init__(self,data):self.data=data
    def raise_for_status(self):pass
    def json(self):return self.data
    def iter_content(self,size):yield json.dumps(self.data).encode()
    def close(self):pass


class GoogleFixture:
    def __init__(self,book):
        rows=[]
        for row in book.active:
            values=[]
            for cell in row:
                value=cell.value
                if isinstance(value,datetime):value=to_excel(value)
                if value is None:values.append({});continue
                typed={'stringValue':value} if isinstance(value,str) else {'numberValue':value}
                entered=typed.copy()
                if cell.coordinate in ['F4','M4']:entered={'formulaValue':'=Dados!'+cell.coordinate}
                values.append({'effectiveValue':typed,'userEnteredValue':entered})
            values.extend({} for _ in range(20-len(values)))
            rows.append({'values':values})
        self.data={'sheets':[{'properties':{'sheetId':7,'title':'Espelho Semanal','gridProperties':{'rowCount':100,'columnCount':20}},'data':[{'rowData':rows}]}]}
        self.calls=[];self.fail_post=False
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def cell(self,row,col):return self.data['sheets'][0]['data'][0]['rowData'][row]['values'][col]
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        if method=='POST':
            if self.fail_post:raise TimeoutError('fixture only')
            for request in kwargs['json']['requests']:
                change=request['updateCells'];region=change['range']
                cell=self.cell(region['startRowIndex'],region['startColumnIndex'])
                value=change['rows'][0]['values'][0]
                fields=change['fields'].split(',')
                if 'userEnteredValue' in fields:
                    cell['userEnteredValue']=copy.deepcopy(value['userEnteredValue'])
                    cell['effectiveValue']=cell['userEnteredValue'].copy()
                    cell.pop('textFormatRuns',None)
                for field in fields:
                    if field=='userEnteredValue':continue
                    target=cell;provided=value
                    parts=field.split('.')
                    for part in parts[:-1]:
                        target=target.setdefault(part,{})
                        provided=provided[part]
                    target[parts[-1]]=copy.deepcopy(provided[parts[-1]])
                    if field==FONT_COLOR_FIELD:
                        cell.setdefault('effectiveFormat',{}).setdefault('textFormat',{})['foregroundColorStyle']=copy.deepcopy(target[parts[-1]])
            return Reply({})
        return Reply(copy.deepcopy(self.data))


class WeeklyWritePilotTests(TestCase):
    def setUp(self):
        self.clock=patch('django.utils.timezone.now',return_value=timezone.make_aware(datetime(2026,11,20,10)))
        self.clock.start();self.addCleanup(self.clock.stop)
        self.admin=get_user_model().objects.create_superuser('pilot-admin')
        self.other_admin=get_user_model().objects.create_superuser('pilot-other-admin')
        self.source=SheetSource.objects.create(spreadsheet_id='pilot-sheet-source-1234567890',name='23/11 a 29/11',format='weekly')
        self.book=Workbook();s=self.book.active;s.title='Espelho Semanal'
        s['B1']=datetime(2026,11,23);s['I1']=datetime(2026,11,24)
        s['B2']='Segunda';s['I2']='Terça';s['A3']='INSTITUTO';s['B3']='Ambulatório';s['I3']='Ambulatório'
        s['C4']='8-12';s['J4']='8-12';s['F4']='Pessoa Um';s['M4']='Pessoa Dois'
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.a=resident_for('Pessoa Um');self.b=resident_for('Pessoa Dois')
        self.offered=Shift.objects.get(resident=self.a);self.requested=Shift.objects.get(resident=self.b)
        self.exchange=propose_exchange(self.a,self.offered.pk,self.requested.pk)
        respond_exchange(self.b,self.exchange.pk,'accept')
        self.pilot=SheetsWritePilot.objects.create(source=self.source,enabled=True,configured_by=self.admin)
        self.connection=SheetsEditorConnection.objects.create(email='fixture-editor@example.test',encrypted_refresh_token='fixture-not-a-real-token',connected_by=self.admin)
        self.google=GoogleFixture(self.book)
        self.session=patch('schedule.sheets_write.authorized_session',return_value=self.google)
        self.session.start();self.addCleanup(self.session.stop)
        self.client.force_login(self.admin)

    def preview(self):return preview_exchange(self.admin,self.exchange.pk)

    def test_preview_reads_only_and_changes_names_not_formulas_until_confirmation(self):
        result=self.preview()
        self.assertEqual(result.state,'preview')
        self.assertEqual([c['address'] for c in result.plan['cells']],['F4','M4'])
        self.assertEqual(result.plan['summary'][0]['after'],'Pessoa Dois')
        self.assertTrue(all(call[0]=='GET' for call in self.google.calls))
        self.assertIn('formulaValue',self.google.cell(3,5)['userEnteredValue'])
        read_fields=self.google.calls[0][2]['params']['fields']
        for field in ['userEnteredFormat(textFormat)','effectiveFormat(textFormat)','textFormatRuns']:
            self.assertIn(field,read_fields)
        self.offered.refresh_from_db();self.assertEqual(self.offered.resident_id,self.b.pk)

    def test_single_batch_updates_only_name_values_and_font_color_and_checks_final_result(self):
        result=apply_preview(self.admin,self.preview().pk)
        self.assertEqual(result.state,'done')
        posts=[c for c in self.google.calls if c[0]=='POST']
        self.assertEqual(len(posts),1)
        writes=posts[0][2]['json']['requests'];self.assertEqual(len(writes),2)
        self.assertTrue(all(i['updateCells']['fields']=='userEnteredValue,'+FONT_COLOR_FIELD for i in writes))
        self.assertEqual(self.google.cell(3,5)['effectiveValue'],{'stringValue':'Pessoa Dois'})
        self.assertEqual(self.google.cell(3,12)['effectiveValue'],{'stringValue':'Pessoa Um'})
        self.assertEqual(self.google.cell(3,2)['effectiveValue'],{'stringValue':'8-12'})
        for column in [5,12]:
            self.assertEqual(self.google.cell(3,column)['userEnteredFormat']['textFormat']['foregroundColorStyle'],{'rgbColor':BLUE_COLOR})
        with self.assertRaises(EditorError):apply_preview(self.admin,result.pk)

    def test_already_applied_is_acknowledged_without_second_write(self):
        apply_preview(self.admin,self.preview().pk)
        previous=len([c for c in self.google.calls if c[0]=='POST'])
        result=self.preview();self.assertTrue(result.plan['already_applied'])
        self.assertEqual(apply_preview(self.admin,result.pk).state,'done')
        self.assertEqual(len([c for c in self.google.calls if c[0]=='POST']),previous)

    def test_name_write_changes_only_value_and_foreground_color_preserving_other_formats(self):
        cell=self.google.cell(3,5)
        untouched={'backgroundColor':{'red':1,'green':.8,'blue':.3},'borders':{'bottom':{'style':'SOLID'}},
                   'horizontalAlignment':'CENTER','verticalAlignment':'MIDDLE','wrapStrategy':'WRAP',
                   'numberFormat':{'type':'TEXT'}}
        font={'fontFamily':'Calibri','fontSize':11,'bold':True,'italic':True,
              'foregroundColorStyle':{'rgbColor':{'red':1}}}
        cell['userEnteredFormat']={**copy.deepcopy(untouched),'textFormat':font}
        cell['textFormatRuns']=[{'startIndex':0,'format':{'bold':True,'foregroundColor':{'green':1}}}]
        self.assertEqual(apply_preview(self.admin,self.preview().pk).state,'done')
        self.assertEqual({key:cell['userEnteredFormat'][key] for key in untouched},untouched)
        self.assertEqual(cell['userEnteredFormat']['textFormat'],{
            **font,'foregroundColorStyle':{'rgbColor':BLUE_COLOR}})
        self.assertNotIn('textFormatRuns',cell)

    def test_new_preview_corrects_only_wrong_font_on_applied_names_preserving_formula(self):
        first=self.google.cell(3,5);second=self.google.cell(3,12)
        first['effectiveValue']={'stringValue':'Pessoa Dois'}
        second['effectiveValue']={'stringValue':'Pessoa Um'}
        first['userEnteredFormat']={'backgroundColor':{'green':.6},'textFormat':{
            'fontSize':12,'bold':True,'foregroundColorStyle':{'rgbColor':{'red':0,'green':0,'blue':0}}}}
        second['userEnteredFormat']={'textFormat':{'foregroundColorStyle':{'rgbColor':BLUE_COLOR.copy()}}}
        original_value=copy.deepcopy(first['userEnteredValue'])
        result=self.preview()
        self.assertTrue(result.plan['already_applied']);self.assertTrue(result.plan['needs_color'])
        page=self.client.get(reverse('sheets_write_review',args=[result.pk]))
        self.assertContains(page,'corrige somente a cor da fonte para azul #0000ff')
        self.assertNotContains(page,'elas serão substituídas')
        self.assertEqual(apply_preview(self.admin,result.pk).state,'done')
        writes=[call for call in self.google.calls if call[0]=='POST']
        self.assertEqual(len(writes),1)
        updates=writes[0][2]['json']['requests'];self.assertEqual(len(updates),1)
        update=updates[0]['updateCells']
        self.assertEqual(update['fields'],FONT_COLOR_FIELD)
        self.assertNotIn('userEnteredValue',update['rows'][0]['values'][0])
        self.assertEqual(first['userEnteredValue'],original_value)
        self.assertEqual(first['effectiveValue'],{'stringValue':'Pessoa Dois'})
        self.assertEqual(first['userEnteredFormat'],{'backgroundColor':{'green':.6},'textFormat':{
            'fontSize':12,'bold':True,'foregroundColorStyle':{'rgbColor':BLUE_COLOR}}})
        done=self.preview();self.assertFalse(done.plan['needs_color'])
        self.assertEqual(apply_preview(self.admin,done.pk).state,'done')
        self.assertEqual(len([call for call in self.google.calls if call[0]=='POST']),1)

    def test_color_only_corrects_rich_text_overrides_preserving_every_other_run_property(self):
        first=self.google.cell(3,5);second=self.google.cell(3,12)
        first['effectiveValue']={'stringValue':'Pessoa Dois'};second['effectiveValue']={'stringValue':'Pessoa Um'}
        for cell in [first,second]:
            cell['userEnteredFormat']={'textFormat':{'foregroundColorStyle':{'rgbColor':BLUE_COLOR.copy()}}}
        first['textFormatRuns']=[
            {'startIndex':0,'format':{'foregroundColorStyle':{'rgbColor':{'red':1}},'bold':True,'fontSize':13}},
            {'startIndex':3,'format':{'foregroundColor':{'red':0},'italic':True,'link':{'uri':'https://example.test'}}},
            {'startIndex':6,'format':{'underline':True}},
            {'startIndex':8,'format':{'foregroundColorStyle':{'rgbColor':BLUE_COLOR.copy()},'strikethrough':True}}]
        original=copy.deepcopy(first['textFormatRuns'])
        result=self.preview();self.assertTrue(result.plan['needs_color'])
        self.assertEqual(apply_preview(self.admin,result.pk).state,'done')
        updates=[call for call in self.google.calls if call[0]=='POST'][0][2]['json']['requests']
        self.assertEqual(len(updates),1)
        self.assertEqual(updates[0]['updateCells']['fields'],FONT_COLOR_FIELD+',textFormatRuns')
        expected=copy.deepcopy(original)
        for run in expected[:2]:
            run['format'].pop('foregroundColor',None)
            run['format']['foregroundColorStyle']={'rgbColor':BLUE_COLOR}
        self.assertEqual(first['textFormatRuns'],expected)
        self.assertIn('formulaValue',first['userEnteredValue'])
        self.assertEqual(apply_preview(self.admin,self.preview().pk).state,'done')
        self.assertEqual(len([call for call in self.google.calls if call[0]=='POST']),1)

    def test_font_or_rich_text_change_after_preview_blocks_stale_write(self):
        for field,value in [('userEnteredFormat',{'textFormat':{'foregroundColorStyle':{'rgbColor':{'green':1}}}}),
                            ('textFormatRuns',[{'startIndex':0,'format':{'bold':True}}])]:
            with self.subTest(field=field):
                result=self.preview();self.google.cell(3,5)[field]=value
                self.assertEqual(apply_preview(self.admin,result.pk).state,'conflict')
                self.assertFalse(any(call[0]=='POST' for call in self.google.calls))

    def test_unconfirmed_color_marks_send_unknown_then_new_preview_can_correct_only_color(self):
        request=self.google.request
        def without_color(method,url,**kwargs):
            result=request(method,url,**kwargs)
            if method=='POST':
                for column in [5,12]:
                    self.google.cell(3,column).pop('userEnteredFormat',None)
                    self.google.cell(3,column).pop('effectiveFormat',None)
            return result
        result=self.preview()
        with patch.object(self.google,'request',side_effect=without_color):
            result=apply_preview(self.admin,result.pk)
        self.assertEqual(result.state,'unknown');self.assertIn('fonte azul #0000ff',result.message)
        retry=self.preview();self.assertTrue(retry.plan['already_applied']);self.assertTrue(retry.plan['needs_color'])
        self.assertEqual(apply_preview(self.admin,retry.pk).state,'done')
        last=[call for call in self.google.calls if call[0]=='POST'][-1]
        self.assertTrue(all(change['updateCells']['fields']==FONT_COLOR_FIELD for change in last[2]['json']['requests']))

    def test_changed_name_time_date_sector_or_formula_blocks_all_writes(self):
        for coordinate,key,value in [((3,5),'effectiveValue',{'stringValue':'Terceira Pessoa'}),
                ((3,2),'effectiveValue',{'stringValue':'9-12'}),
                ((0,1),'effectiveValue',{'numberValue':to_excel(datetime(2026,11,25))}),
                ((2,1),'effectiveValue',{'stringValue':'Setor alterado'}),
                ((3,5),'userEnteredValue',{'formulaValue':'=OutraAba!F4'})]:
            with self.subTest(coordinate=coordinate,key=key):
                self.google=GoogleFixture(self.book);self.session.stop()
                self.session=patch('schedule.sheets_write.authorized_session',return_value=self.google);self.session.start()
                result=self.preview();self.google.cell(*coordinate)[key]=value
                result=apply_preview(self.admin,result.pk)
                self.assertEqual(result.state,'conflict')
                self.assertFalse(any(c[0]=='POST' for c in self.google.calls))

    def test_half_applied_swap_is_not_overwritten(self):
        self.google.cell(3,5)['effectiveValue']={'stringValue':'Pessoa Dois'}
        with self.assertRaisesMessage(EditorError,'Só uma atividade'):self.preview()
        self.assertFalse(any(c[0]=='POST' for c in self.google.calls))

    def test_post_timeout_is_unknown_and_cannot_be_retried_blindly(self):
        result=self.preview();self.google.fail_post=True
        result=apply_preview(self.admin,result.pk)
        self.assertEqual(result.state,'unknown')
        with self.assertRaises(EditorError):apply_preview(self.admin,result.pk)

    def test_expired_preview_disabled_pilot_and_changed_google_connection_block(self):
        result=self.preview()
        SheetsWritePreview.objects.filter(pk=result.pk).update(created_at=timezone.now()-timedelta(minutes=11))
        with self.assertRaises(EditorError):apply_preview(self.admin,result.pk)
        result=self.preview();self.pilot.enabled=False;self.pilot.save()
        self.assertEqual(apply_preview(self.admin,result.pk).state,'conflict')
        self.pilot.enabled=True;self.pilot.save()
        result=self.preview();SheetsEditorConnection.objects.filter(pk=self.connection.pk).update(connected_at=timezone.now()+timedelta(seconds=1))
        self.assertEqual(apply_preview(self.admin,result.pk).state,'conflict')

    def test_scope_blocks_other_week_monthly_different_source_pending_or_reused_exchange(self):
        for changes in [{'source':'another-source'}, {'category':'night'}, {'requires_reconciliation':True},
                        {'starts_at':timezone.make_aware(datetime(2026,11,30,8)),'ends_at':timezone.make_aware(datetime(2026,11,30,12))}, {'source_tab':'R1'}]:
            with self.subTest(changes=changes):
                old={k:getattr(self.offered,k) for k in changes}
                Shift.objects.filter(pk=self.offered.pk).update(**changes)
                with self.assertRaises(EditorError):self.preview()
                Shift.objects.filter(pk=self.offered.pk).update(**old)
        from .models import Exchange
        Exchange.objects.filter(pk=self.exchange.pk).update(status='pending')
        with self.assertRaises(EditorError):self.preview()
        Exchange.objects.filter(pk=self.exchange.pk).update(status='accepted')
        Exchange.objects.create(offered=self.offered,requested=self.requested,proposer=self.a,recipient=self.b,status='accepted')
        with self.assertRaisesMessage(EditorError,'mais de uma troca'):self.preview()
        self.assertFalse(any(c[0]=='POST' for c in self.google.calls))

    def test_resident_third_party_csrf_and_confirmation_are_enforced(self):
        result=self.preview()
        self.client.force_login(self.a)
        for url in [reverse('sheets_write_pilot'),reverse('sheets_write_review',args=[result.pk])]:
            self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.post(reverse('sheets_write_apply',args=[result.pk]),{'confirm':'yes'}).status_code,403)
        self.client.force_login(self.other_admin)
        self.assertEqual(self.client.get(reverse('sheets_write_review',args=[result.pk])).status_code,404)
        strict=Client(enforce_csrf_checks=True);strict.force_login(self.admin)
        self.assertEqual(strict.post(reverse('sheets_write_apply',args=[result.pk]),{'confirm':'yes'}).status_code,403)
        self.client.force_login(self.admin)
        self.client.post(reverse('sheets_write_apply',args=[result.pk]),{})
        result.refresh_from_db();self.assertEqual(result.state,'preview')
        self.assertFalse(any(c[0]=='POST' for c in self.google.calls))

    def test_management_get_has_no_remote_side_effects_and_source_needs_no_url(self):
        response=self.client.get(reverse('sheets_write_pilot'))
        self.assertContains(response,self.source.name)
        self.assertIn('no-store',response['Cache-Control'])
        self.assertEqual(self.google.calls,[])
        self.client.post(reverse('sheets_write_configure'),{'source':self.source.pk})
        self.pilot.refresh_from_db();self.assertTrue(self.pilot.enabled)
        self.assertEqual(self.pilot.source_id,self.source.pk)

    def test_source_acknowledgement_preserves_ids_calendar_uids_and_no_duplicates(self):
        original=set(Shift.objects.values_list('pk',flat=True))
        apply_preview(self.admin,self.preview().pk)
        self.book.active['F4']='Pessoa Dois';self.book.active['M4']='Pessoa Um'
        for _ in range(2):
            summary=sync_workbook(self.book,self.source.spreadsheet_id)
            self.assertIn('0 exigem conciliação',summary)
            self.assertEqual(set(Shift.objects.values_list('pk',flat=True)),original)
            self.assertEqual(Shift.objects.filter(active=True).count(),2)
        self.offered.refresh_from_db();self.requested.refresh_from_db()
        self.assertEqual(self.offered.resident_id,self.b.pk);self.assertEqual(self.requested.resident_id,self.a.pk)

    def test_stale_read_after_acknowledgement_does_not_restore_old_owners_or_duplicate(self):
        original=set(Shift.objects.values_list('pk',flat=True))
        self.book.active['F4']='Pessoa Dois';self.book.active['M4']='Pessoa Um'
        sync_workbook(self.book,self.source.spreadsheet_id)
        self.book.active['F4']='Pessoa Um';self.book.active['M4']='Pessoa Dois'
        summary=sync_workbook(self.book,self.source.spreadsheet_id)
        self.assertIn('0 exigem conciliação',summary)
        self.assertEqual(set(Shift.objects.values_list('pk',flat=True)),original)
        self.offered.refresh_from_db();self.requested.refresh_from_db()
        self.assertEqual(self.offered.resident_id,self.b.pk)
        self.assertEqual(self.requested.resident_id,self.a.pk)

    def test_duplicate_names_are_both_written_but_blanks_remain_blank(self):
        self.google.cell(3,6).update(copy.deepcopy(self.google.cell(3,5)))
        result=self.preview();self.assertEqual(len(result.plan['cells']),3)
        self.assertEqual(apply_preview(self.admin,result.pk).state,'done')
        self.assertEqual(self.google.cell(3,6)['effectiveValue']['stringValue'],'Pessoa Dois')
        self.assertEqual(self.google.cell(3,13),{})

    def test_live_write_blocks_second_sender_but_interrupted_write_can_be_checked_later(self):
        old=self.preview()
        SheetsWritePreview.objects.filter(pk=old.pk).update(state='writing',started_at=timezone.now())
        new=self.preview()
        with self.assertRaisesMessage(EditorError,'em andamento'):
            apply_preview(self.admin,new.pk)
        self.assertFalse(any(c[0]=='POST' for c in self.google.calls))
        SheetsWritePreview.objects.filter(pk=old.pk).update(started_at=timezone.now()-timedelta(minutes=11))
        self.assertEqual(apply_preview(self.admin,new.pk).state,'done')
        old.refresh_from_db();self.assertEqual(old.state,'unknown')

    def test_disabled_resident_behind_editor_admin_invalidates_connection_for_all_admins(self):
        from .models import ResidentAccess
        retired=get_user_model().objects.create_user('retired-editor-identity',is_active=False)
        ResidentAccess.objects.create(account=self.admin,resident=retired)
        with self.assertRaisesMessage(EditorError,'Conecte uma conta Google'):
            preview_exchange(self.other_admin,self.exchange.pk)
        self.assertFalse(self.google.calls)

    def test_another_admin_cannot_use_personal_google_connection(self):
        with self.assertRaises(EditorError):preview_exchange(self.other_admin,self.exchange.pk)
        self.assertFalse(self.google.calls)


class GoogleEditorAuthorizationTests(TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        folder=Path(self.directory.name)
        self.key=Fernet.generate_key();self.key_path=folder/'key.txt';self.key_path.write_bytes(self.key)
        self.client_path=folder/'client.json'
        self.client_path.write_text(json.dumps({'web':{'client_id':'fictitious-client-id','client_secret':'fictitious-client-secret',
            'redirect_uris':['https://testserver/google/planilhas/retorno/']}}))
        self.environment=patch.dict(os.environ,{'GOOGLE_SHEETS_TOKEN_KEY_FILE':str(self.key_path),'GOOGLE_SHEETS_OAUTH_CLIENT_FILE':str(self.client_path)})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.admin=get_user_model().objects.create_superuser('oauth-fixture-admin')
        self.client.force_login(self.admin)

    def begin(self):
        response=self.client.post(reverse('google_editor_connect'),secure=True)
        self.assertEqual(response.status_code,302)
        self.assertTrue(response.url.startswith('https://accounts.google.com/'))
        return parse_qs(urlsplit(response.url).query)

    def grant(self,state,**changes):
        payload={'scope':' '.join(SCOPES),'refresh_token':'fictitious-refresh-token','access_token':'fictitious-access-token'}
        payload.update(changes)
        with patch('schedule.google_editor.requests.post',return_value=Reply(payload)) as post:
            with patch('schedule.google_editor.requests.get',return_value=Reply({'email':'editor@example.test','verified_email':True})):
                response=self.client.get(reverse('google_editor_callback'),{'state':state,'code':'fictitious-authorization-code'},secure=True)
        return response,post

    def test_authorization_uses_state_pkce_and_stores_only_encrypted_refresh_token(self):
        params=self.begin()
        self.assertEqual(params['code_challenge_method'],['S256'])
        self.assertEqual(params['access_type'],['offline'])
        response,post=self.grant(params['state'][0])
        self.assertEqual(response.status_code,302)
        connection=SheetsEditorConnection.objects.get()
        self.assertNotIn('fictitious-refresh-token',connection.encrypted_refresh_token)
        self.assertEqual(Fernet(self.key).decrypt(connection.encrypted_refresh_token.encode()),b'fictitious-refresh-token')
        self.assertEqual(connection.email,'editor@example.test')
        self.assertEqual(post.call_args.kwargs['data']['grant_type'],'authorization_code')
        self.assertIn('code_verifier',post.call_args.kwargs['data'])
        self.assertNotIn('sheets_oauth',self.client.session)
        with patch('schedule.google_editor.requests.post') as duplicate:
            self.client.get(reverse('google_editor_callback'),{'state':params['state'][0],'code':'replayed'},secure=True)
            duplicate.assert_not_called()

    def test_wrong_expired_or_different_account_state_cannot_redeem_code(self):
        for mode in ['wrong','expired','account']:
            self.begin();session=self.client.session;saved=session['sheets_oauth']
            state=saved['state']
            if mode=='wrong':state='attacker-state'
            elif mode=='expired':saved['time']=0
            else:saved['account']=self.admin.pk+1
            session['sheets_oauth']=saved;session.save()
            with patch('schedule.google_editor.requests.post') as provider:
                self.client.get(reverse('google_editor_callback'),{'state':state,'code':'fixture-code'},secure=True)
                provider.assert_not_called()
        self.assertFalse(SheetsEditorConnection.objects.exists())

    def test_readonly_or_missing_refresh_grant_is_not_connected(self):
        for change in [{'scope':'https://www.googleapis.com/auth/drive.readonly'}, {'refresh_token':''}]:
            state=self.begin()['state'][0]
            self.grant(state,**change)
            self.assertFalse(SheetsEditorConnection.objects.exists())

    def test_nonadmin_cannot_connect_callback_or_disconnect_and_connection_requires_post(self):
        self.assertEqual(self.client.get(reverse('google_editor_connect'),secure=True).status_code,405)
        resident=get_user_model().objects.create_user('oauth-non-admin')
        self.client.force_login(resident)
        self.assertEqual(self.client.post(reverse('google_editor_connect'),secure=True).status_code,403)
        self.assertEqual(self.client.get(reverse('google_editor_callback'),secure=True).status_code,403)
        self.assertEqual(self.client.post(reverse('google_editor_disconnect'),secure=True).status_code,403)

    def test_provider_failure_is_generic_and_contains_no_code_token_or_secret(self):
        state=self.begin()['state'][0]
        with patch('schedule.google_editor.requests.post',side_effect=RuntimeError('fictitious-secret fixture-code')):
            response=self.client.get(reverse('google_editor_callback'),{'state':state,'code':'fixture-code'},secure=True,follow=True)
        self.assertNotContains(response,'fictitious-secret')
        self.assertNotContains(response,'fixture-code')
        self.assertFalse(SheetsEditorConnection.objects.exists())

    def test_disconnect_disables_pilot_and_removes_local_token(self):
        self.grant(self.begin()['state'][0])
        source=SheetSource.objects.create(spreadsheet_id='oauth-fixture-weekly-1234567890')
        SheetsWritePilot.objects.create(source=source,enabled=True,configured_by=self.admin)
        self.client.post(reverse('google_editor_disconnect'),secure=True)
        self.assertFalse(SheetsEditorConnection.objects.exists())
        self.assertFalse(SheetsWritePilot.objects.get().enabled)

    def test_callback_log_atoms_redact_codes_state_and_encoded_referer(self):
        from config.access_logging import redact_sensitive_text
        for value in ['/google/planilhas/retorno/?code=fictional-code&state=fictional-state',
                      'https://app.example.test/google/planilhas/retorno/?code=fictional-code&state=fictional-state',
                      '%2Fgoogle%2Fplanilhas%2Fretorno%2F?code=fictional-code&state=fictional-state']:
            safe=redact_sensitive_text(value)
            self.assertNotIn('fictional-code',safe);self.assertNotIn('fictional-state',safe)
