from datetime import datetime, timedelta
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook

from .cinderelas_import import sync_cinderelas, sync_plantoes
from .models import SheetSource, Shift, SyncHeartbeat


class AutomaticPlantaoTests(TestCase):
    def setUp(self):
        self.source=SheetSource.objects.get(format='plantoes')
        self.a=get_user_model().objects.create_user('auto-plantao-a',first_name='Plantonista Um')
        self.b=get_user_model().objects.create_user('auto-plantao-b',first_name='Plantonista Dois')
        start=timezone.make_aware(datetime(2040,1,1,8))
        for person in [self.a,self.b]:
            Shift.objects.create(external_id=f'auto-roster-{person.pk}',source='regular-roster',resident=person,starts_at=start,ends_at=start+timedelta(hours=4),sector='Ambulatório')

    def book(self):
        book=Workbook();sheet=book.active;sheet.title='JANEIRO2035'
        for col,day in zip(range(2,21,3),['SEGUNDA','TERÇA','QUARTA','QUINTA','SEXTA','SÁBADO','DOMINGO']):
            sheet.cell(2,col,day)
        sheet['A4']='R1'
        for col,day,hours,name in [(2,1,'18-24','Plantonista Um'),(5,2,'18-8','Plantonista Dois'),(17,6,'8-8','Plantonista Um')]:
            sheet.cell(3,col,datetime(2035,1,day));sheet.cell(4,col,hours);sheet.cell(4,col+1,name)
        sheet['Q5']='8-20';sheet['R5']='Plantonista Dois'
        return book

    def test_migration_registers_source_and_queues_read_without_user_url(self):
        self.assertTrue(self.source.enabled)
        self.assertIsNotNone(SyncHeartbeat.objects.get(key='sheets').refresh_requested_at)
        registration=import_module('schedule.migrations.0016_automatic_plantao_source').register_plantao_source
        registration(apps,SimpleNamespace(connection=connection))
        self.assertEqual(SheetSource.objects.filter(spreadsheet_id=self.source.spreadsheet_id).count(),1)
        self.source.refresh_from_db();self.assertEqual(self.source.format,'plantoes')

    def test_full_import_preserves_existing_cinderela_ids_and_adds_nights_weekends(self):
        book=self.book();sync_cinderelas(book,self.source.spreadsheet_id)
        original=Shift.objects.get(source=self.source.spreadsheet_id)
        sync_plantoes(book,self.source.spreadsheet_id)
        shifts=Shift.objects.filter(source=self.source.spreadsheet_id)
        self.assertEqual(shifts.count(),4)
        self.assertEqual(shifts.get(category='cinderela').pk,original.pk)
        self.assertEqual(set(shifts.values_list('category',flat=True)),{'cinderela','night','weekend'})
        self.assertEqual(sorted(int((s.ends_at-s.starts_at).total_seconds()/3600) for s in shifts),[6,12,14,24])
        ids=set(shifts.values_list('pk',flat=True));sync_plantoes(book,self.source.spreadsheet_id)
        self.assertEqual(set(shifts.values_list('pk',flat=True)),ids)

    def test_registered_worker_imports_all_types_without_manual_registration(self):
        with patch('schedule.management.commands.sync_sheets.fetch_workbook',return_value=self.book()) as fetch:
            call_command('sync_sheets',registered=True,stdout=StringIO())
        fetch.assert_called_once_with(self.source.spreadsheet_id,inspect_formulas=True)
        self.assertEqual(Shift.objects.filter(source=self.source.spreadsheet_id,active=True).count(),4)

    def test_calendar_and_export_show_overnight_end_day_and_all_types(self):
        sync_plantoes(self.book(),self.source.spreadsheet_id)
        self.client.force_login(self.a)
        page=self.client.get('/',{'week':'2035-01-01','scope':'all'})
        self.assertContains(page,'Noturno / 24h')
        self.assertContains(page,'Diurno / fim de semana')
        self.assertContains(page,'dia seguinte')
        ics=self.client.get('/calendario.ics').content.decode()
        self.assertIn('Cinderela',ics);self.assertIn('Plantão 24h',ics)
        self.assertNotIn('Plantão diurno',ics)  # Assigned only to the other resident.
