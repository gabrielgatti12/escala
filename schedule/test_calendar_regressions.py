from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import Allocation, Exchange, IncompleteActivity, SharedActivity, Shift, SheetSource, SyncState


class CalendarNavigationTests(TestCase):
    def setUp(self):
        self.person = get_user_model().objects.create_user('calendar-person')
        self.other = get_user_model().objects.create_user('calendar-other')
        self.client.force_login(self.person)
        self.start = timezone.localtime(timezone.now() + timedelta(days=8)).replace(hour=8, minute=0, second=0, microsecond=0)

    def activity(self, key, start):
        item = SharedActivity.objects.create(external_id=key, source='calendar-test', starts_at=start, ends_at=start + timedelta(hours=4), sector='Refração')
        item.residents.set([self.person, self.other])
        return item

    def test_week_opens_shared_activity_before_later_defined_shift(self):
        shared = self.activity('upcoming-shared', self.start)
        Shift.objects.create(external_id='later-defined', resident=self.person, starts_at=self.start + timedelta(days=21), ends_at=self.start + timedelta(days=21, hours=4), sector='Outro setor')
        response = self.client.get('/')
        day = timezone.localdate(shared.starts_at)
        self.assertEqual(response.context['week_start'], day - timedelta(days=day.weekday()))
        self.assertIn(shared, [item for day in response.context['days'] for item in day['shared']])

    def test_week_opens_missing_time_activity_when_no_defined_shifts(self):
        item = IncompleteActivity.objects.create(external_id='upcoming-incomplete', source='calendar-test', source_cell='C4:G4', date=timezone.localdate(self.start), sector='Patologia')
        item.residents.add(self.person)
        response = self.client.get('/')
        self.assertIn(item, [item for day in response.context['days'] for item in day['incomplete']])

    def test_dialog_excludes_historical_divisions_but_calendar_keeps_them(self):
        past = self.start - timedelta(days=30)
        first = self.activity('past-first', past)
        self.activity('past-second', past)
        future = self.activity('future-first', self.start)
        other = self.activity('future-second', self.start)
        response = self.client.get('/', {'week':timezone.localdate(past).isoformat()})
        self.assertEqual({item['activity'].pk for item in response.context['allocation_options']}, {future.pk, other.pk})
        self.assertIn(first, [item for day in response.context['days'] for item in day['shared']])

    def test_expired_requests_leave_inbox_before_next_sync_cycle(self):
        start = self.start - timedelta(days=30)
        offered = Shift.objects.create(external_id='past-offer', resident=self.other, starts_at=start, ends_at=start + timedelta(hours=4), sector='Hospital')
        requested = Shift.objects.create(external_id='future-request', resident=self.person, starts_at=self.start, ends_at=self.start + timedelta(hours=4), sector='Instituto')
        exchange = Exchange.objects.create(offered=offered, requested=requested, proposer=self.other, recipient=self.person)
        mine = self.activity('expired-mine', start)
        theirs = self.activity('expired-theirs', start)
        Allocation.objects.create(mine=mine, theirs=theirs, proposer=self.other, recipient=self.person)
        response = self.client.get('/?tab=requests')
        self.assertEqual(response.context['received_count'], 0)
        self.assertEqual(list(response.context['exchanges']), [])
        self.assertEqual(list(response.context['allocations']), [])
        history = self.client.get('/?tab=requests&box=history')
        self.assertContains(history, 'Prazo encerrado', count=2)
        self.assertNotContains(history, 'Aceitar troca')
        self.assertNotContains(history, 'Aceitar divisão e atualizar calendário')
        exchange.refresh_from_db()
        self.assertEqual(exchange.status, 'pending')  # GET does not mutate requests.

    def test_month_navigation_preserves_scope_across_year_boundary(self):
        response = self.client.get('/calendario/?month=2026-12&all=1')
        self.assertContains(response, '?month=2027-01&amp;all=1')
        self.assertContains(response, '?month=2026-11&amp;all=1')
        self.assertContains(response, '/?week=2026-12-01&amp;scope=all')
        self.assertContains(response, 'Nenhum plantão definido neste mês')

    def test_extreme_calendar_dates_do_not_crash_navigation(self):
        for path in ['/?week=9999-12-31', '/?week=0001-01-01', '/calendario/?month=9999-12', '/calendario/?month=0001-01']:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_week_date_form_preserves_resident_and_interest_filters(self):
        response = self.client.get('/', {'scope':'all', 'resident':self.other.pk, 'interest':'1'})
        self.assertContains(response, f'name="resident" value="{self.other.pk}"')
        self.assertContains(response, 'name="interest" value="1"')

    def test_source_reconciliation_and_parser_details_are_visible_to_admin(self):
        self.person.is_staff = self.person.is_superuser = True
        self.person.save()
        SheetSource.objects.create(spreadsheet_id='source-with-reconciliation', name='Semana de teste', error='Linha 4: setor não identificado.')
        SyncState.objects.create(source='source-with-reconciliation', last_success=timezone.now(), summary='2 plantões definidos; 1 exigem conciliação.')
        response = self.client.get('/?tab=sources')
        self.assertContains(response, '1 exigem conciliação.')
        self.assertContains(response, 'Linha 4: setor não identificado.')

    def test_allocation_actions_return_to_corresponding_request_box(self):
        mine = self.activity('redirect-first', self.start)
        theirs = self.activity('redirect-second', self.start)
        response = self.client.post('/divisoes/criar/', {'mine':mine.pk, 'theirs':theirs.pk})
        self.assertRedirects(response, '/?tab=requests&box=sent')
        proposal = Allocation.objects.get()
        self.client.force_login(self.other)
        self.assertRedirects(self.client.post(f'/divisoes/{proposal.pk}/reject/'), '/?tab=requests&box=received')
