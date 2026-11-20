from datetime import date, datetime, timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from .live import data_revision
from .models import BundleExchange, Exchange, SharedActivity, Shift
from .presentation import activity_style, month_sector_groups, sector_hue, sector_rows


class SourceActivityPresentationTests(SimpleTestCase):
    def setUp(self):
        self.start=timezone.make_aware(datetime(2035,1,1,8))
        self.person=SimpleNamespace(pk=1,first_name='Pessoa',username='pessoa',get_full_name=lambda:'Pessoa Fictícia')
        self.request=RequestFactory().get('/calendario/')
        self.request.user=AnonymousUser()

    def activity(self,**changes):
        fields=dict(pk=1,sector='INSTITUTO · Ambulatório',source_cell='C4:G4',
                    source_background='#0b5394',source_foreground='#ffffff',notes='Cob R1\nINTERCONSULTA HVL',
                    starts_at=self.start,ends_at=self.start+timedelta(hours=4),resident_id=1,resident=self.person,
                    source_url='',wants_exchange=False,requires_reconciliation=False,category='regular',
                    is_rest=False,residents=SimpleNamespace(all=lambda:[self.person]))
        fields.update(changes)
        return SimpleNamespace(**fields)

    def render_cards(self,template,item,**extra):
        return render_to_string('components/'+template,{
            'items':[item],'resident_user':self.person,'scope':'all','now':self.start-timedelta(days=1),
            'allocation_eligible_ids':set(),**extra,
        },request=self.request)

    def test_source_style_uses_literal_rgb_and_retains_old_fallback(self):
        item=self.activity()
        style=activity_style(item)
        self.assertIn('--source-bg:#0B5394',style)
        self.assertIn('--source-fg:#FFFFFF',style)
        self.assertIn('--sector-hue:'+sector_hue(item.sector),style)
        self.assertEqual(activity_style(SimpleNamespace(sector=item.sector)),
                         '--sector-hue:'+sector_hue(item.sector)+';--source-bg:initial;--source-fg:initial')

    def test_untrusted_style_values_cannot_escape_into_css(self):
        for invalid in ['red','url(https://example.test/x)','#123456;position:fixed','#123','"><script>',' #123456',None]:
            with self.subTest(value=invalid):
                style=activity_style(self.activity(source_background=invalid,source_foreground=invalid))
                self.assertIn('--source-bg:initial',style)
                self.assertIn('--source-fg:initial',style)
                self.assertNotIn('position:',style)
                self.assertNotIn('script',style)

    def test_defined_shared_and_missing_time_cards_show_notes_and_original_colors(self):
        for template in ['shift_cards.html','shared_cards.html','incomplete_cards.html']:
            with self.subTest(template=template):
                html=self.render_cards(template,self.activity())
                self.assertIn('--source-bg:#0B5394',html)
                self.assertIn('--source-fg:#FFFFFF',html)
                self.assertIn('Cob R1<br>INTERCONSULTA HVL',html)
                self.assertIn('Obs.:',html)

    def test_notes_are_escaped_and_still_visible_in_grouped_cards(self):
        html=self.render_cards('shift_cards.html',self.activity(notes='<script>alert(1)</script>\nCob R1'),grouped=True)
        self.assertNotIn('<script>alert(1)</script>',html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;<br>Cob R1',html)
        self.assertNotIn('<h3>',html)  # Title is in the sector header; note remains per slot.

    def test_group_header_prefers_first_colored_source_row_without_changing_group_or_order(self):
        no_color=self.activity(source_background='',source_foreground='',source_cell='C3:G3')
        later=self.activity(pk=2,source_background='#A64D79',source_cell='C51:G51')
        earlier=self.activity(pk=3,source_background='#6AA84F',source_cell='C43:G43')
        items=[no_color,later,earlier]
        days=[{'date':self.start.date(),'shifts':items,'shared':[],'incomplete':[]}]
        week=sector_rows(days)
        month=month_sector_groups(items)
        for group in [week[0],month[0]]:
            self.assertIn('--source-bg:#6AA84F',group['style'])
            self.assertEqual(group['name'],no_color.sector)
            self.assertEqual(group['order'],3)
        self.assertEqual(week[0]['cells'][0]['count'],3)
        self.assertEqual([item.pk for item in month[0]['shifts']],[1,2,3])

    def test_monthly_view_keeps_observations_and_source_palette(self):
        item=self.activity(notes='INTERCONSULTA HVL <confirmar>')
        html=render_to_string('calendar.html',{
            'weeks':[[(1,month_sector_groups([item]))]],'month':date(2035,1,1),
            'previous_month':date(2034,12,1),'next_month':date(2035,2,1),'today':date(2035,1,1),
            'has_shifts':True,
        },request=self.request)
        self.assertIn('--source-bg:#0B5394',html)
        self.assertIn('INTERCONSULTA HVL &lt;confirmar&gt;',html)
        self.assertNotIn('HVL <confirmar>',html)

    def test_allocation_options_and_division_review_include_observations(self):
        activity=SharedActivity(pk=1,starts_at=self.start,ends_at=self.start+timedelta(hours=4),
                                sector='INSTITUTO · Ambulatório',notes='Cob R1 <conferir>')
        html=render_to_string('components/allocation_dialog.html',{
            'my_shared':[activity],'allocation_options':[{
                'activity':activity,'pair':'1,2','day':'2035-01-01','start':self.start.isoformat(),
                'end':(self.start+timedelta(hours=4)).isoformat(),
            }],
        },request=self.request)
        self.assertEqual(html.count('— Obs.: Cob R1 &lt;conferir&gt;'),2)
        proposal=SimpleNamespace(pk=1,mine=activity,theirs=activity,proposer=self.person,recipient=self.person,
                                 status='accepted',get_status_display=lambda:'Aceita',is_expired=False)
        html=render_to_string('components/allocations.html',{'allocations':[proposal]},request=self.request)
        self.assertEqual(html.count('Cob R1 &lt;conferir&gt;'),2)

    def test_day_exchange_composer_and_report_include_observations(self):
        item=self.activity(display_sector='Ambulatório · INSTITUTO',notes='INTERCONSULTA HVL <conferir>')
        report={'offered':[vars(item)],'requested':[vars(item)],'proposer':{'name':'Pessoa A'},
                'recipient':{'name':'Pessoa B'},'warnings':[]}
        class EmptyForm(list):
            cleaned_data={'mode':'day','recipient':self.person,'offered_day':self.start.date(),
                          'requested_day':self.start.date()}
        html=render_to_string('bundle_compose.html',{
            'loaded':True,'form':EmptyForm(),'offered':[item],'requested':[item],
            'now':self.start-timedelta(days=1),
        },request=self.request)
        self.assertEqual(html.count('INTERCONSULTA HVL &lt;conferir&gt;'),2)
        html=render_to_string('components/bundle_report.html',{'report':report},request=self.request)
        self.assertEqual(html.count('INTERCONSULTA HVL &lt;conferir&gt;'),2)

    def test_reverted_requests_and_bundle_detail_explain_the_transfer_and_external_undo(self):
        offered=self.activity(notes='Cob R1 <conferir>')
        proposal=SimpleNamespace(pk=1,proposer=self.person,recipient=self.person,recipient_id=2,
                                 offered=offered,requested=offered,status='reverted',is_expired=False,
                                 get_status_display=lambda:'Desfeita na planilha',get_mode_display=lambda:'Dia inteiro',
                                 source_reverted_at=self.start,proposer_acknowledged_at=self.start,
                                 snapshot={'offered':[vars(offered)],'requested':[vars(offered)]})
        for template,context in [
            ('components/requests.html',{'exchanges':[proposal]}),
            ('components/bundle_requests.html',{'bundles':[proposal]}),
            ('bundle_review.html',{'bundle':proposal,'stage':'detail','report':{'warnings':[]}}),
        ]:
            with self.subTest(template=template):
                html=render_to_string(template,{**context,'resident_user':self.person},request=self.request)
                self.assertIn('Desfeita na planilha',html)
                self.assertIn('as atividades foram transferidas',html)
                self.assertIn('reversão foi confirmada',html)
                self.assertNotIn('encerrada sem transferir',html)
                self.assertNotIn('Aceitar troca',html)
                self.assertNotIn('aceito a troca completa',html)
                if 'requests.html' in template:
                    self.assertIn('Cob R1 &lt;conferir&gt;',html)

    def test_reverted_sheet_preview_is_history_without_an_active_write_plan(self):
        decision=SimpleNamespace(source_reverted_at=self.start)
        for field in ['exchange','bundle']:
            with self.subTest(decision=field):
                preview=SimpleNamespace(pk=1,state='reverted',get_state_display=lambda:'Desfeita na planilha',
                    decision_label='Troca #1',source=SimpleNamespace(spreadsheet_id='source-test'),
                    plan={'already_applied':True,'needs_color':False,'email':'test@example.test',
                          'summary':[{'date':'01/01/2035','sector':'Ambulatório','before':'Pessoa A','after':'Pessoa B'}],
                          'cells':[{'address':'F4'}]},message='Reversão confirmada na origem <conferida>',
                    **{'exchange':None,'bundle':None,field:decision})
                html=render_to_string('sheets_write_review.html',{'preview':preview},request=self.request)
                self.assertIn('Histórico da gravação',html)
                self.assertIn('Reversão confirmada em',html)
                self.assertIn('Reversão confirmada na origem &lt;conferida&gt;',html)
                self.assertIn('Nomes gravados naquela troca',html)
                self.assertNotIn('A planilha já reflete esta troca',html)
                self.assertNotIn('Após gravar:',html)
                self.assertNotIn('name="confirm"',html)
                self.assertNotIn('/aplicar/',html)


class RevertedLiveRevisionTests(TestCase):
    def test_external_reversal_refreshes_open_calendars_for_each_exchange_type(self):
        start=timezone.make_aware(datetime(2035,1,1,8))
        people=[get_user_model().objects.create_user(username='source-reverted-'+name) for name in ['a','b']]
        shifts=[Shift.objects.create(external_id='source-reverted-'+str(index),resident=person,
            starts_at=start,ends_at=start+timedelta(hours=4),sector='INSTITUTO · Ambulatório')
            for index,person in enumerate(people)]
        decisions=[
            Exchange.objects.create(offered=shifts[0],requested=shifts[1],proposer=people[0],
                                    recipient=people[1],status='accepted',resolved_at=start),
            BundleExchange.objects.create(proposer=people[0],recipient=people[1],mode='day',
                status='accepted',resolved_at=start,proposer_acknowledged_at=start),
        ]
        for decision in decisions:
            with self.subTest(model=type(decision).__name__):
                before=data_revision()
                type(decision).objects.filter(pk=decision.pk).update(
                    status='reverted',source_reverted_at=start+timedelta(days=1))
                self.assertNotEqual(before,data_revision())
