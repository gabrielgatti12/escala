from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import Shift


class CalendarLoadingTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user('fast-calendar-person')
        self.other = get_user_model().objects.create_user('fast-calendar-other')
        self.client.force_login(self.person)
        self.start = datetime(2035, 1, 1, 8, tzinfo=ZoneInfo('America/Sao_Paulo'))
        for person in [self.person, self.other]:
            Shift.objects.create(external_id=f'fast-own-{person.pk}', resident=person,
                                 starts_at=self.start, ends_at=self.start + timedelta(hours=4), sector='HOSPITAL · PA')

    def test_week_and_requests_do_not_embed_every_future_colleague_shift(self):
        paths = ['/?week=2035-01-01', '/?tab=requests', '/mural/']
        before = {path: len(self.client.get(path).content) for path in paths}
        Shift.objects.bulk_create([
            Shift(external_id=f'fast-future-{i}', resident=self.other,
                  starts_at=self.start + timedelta(days=8+i),
                  ends_at=self.start + timedelta(days=8+i, hours=4), sector='Unique remote shift')
            for i in range(1000)
        ])
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertLess(abs(len(response.content)-before[path]), 2000)
                self.assertNotContains(response, 'Unique remote shift')

    def test_administrative_tabs_do_not_load_week_or_allocation_choices(self):
        self.person.is_superuser = self.person.is_staff = True
        self.person.save()
        with patch('schedule.calendar_data.next_activity_day', side_effect=AssertionError('calendar scan')):
            with patch('schedule.calendar_data.allocation_choices', side_effect=AssertionError('allocation scan')):
                for tab in ['accounts', 'sources']:
                    self.assertEqual(self.client.get('/', {'tab':tab}).status_code, 200)

    def test_local_week_and_month_include_late_night_before_boundary(self):
        # Local Sunday night is already Monday in UTC. Monday midnight must be
        # outside this weekly window and February midnight outside January.
        local = ZoneInfo('America/Sao_Paulo')
        sunday = datetime(2035, 1, 7, 23, 30, tzinfo=local)
        jan_end = datetime(2035, 1, 31, 23, 30, tzinfo=local)
        for key, start in [('sunday',sunday), ('monday',sunday+timedelta(minutes=30)),
                           ('january',jan_end), ('february',jan_end+timedelta(minutes=30))]:
            Shift.objects.create(external_id=f'boundary-{key}', resident=self.person,
                                 starts_at=start, ends_at=start+timedelta(hours=1), sector=key)
        with timezone.override(local):
            week = self.client.get('/', {'week':'2035-01-01'})
            ids = {s.external_id for day in week.context['days'] for s in day['shifts']}
            self.assertIn('boundary-sunday', ids)
            self.assertNotIn('boundary-monday', ids)
            month = self.client.get('/calendario/', {'month':'2035-01'})
            ids = {s.external_id for week in month.context['weeks'] for _,groups in week
                   for group in groups for s in group['shifts']}
            self.assertIn('boundary-january', ids)
            self.assertNotIn('boundary-february', ids)
