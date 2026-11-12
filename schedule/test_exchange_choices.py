from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import Shift


class ExchangeChoiceTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.me = users.objects.create_user('choices-person', first_name='Primeira Pessoa')
        self.other = users.objects.create_user('choices-other', first_name='José', last_name='Ávila')
        self.third = users.objects.create_user('choices-third', first_name='Outra Pessoa')
        self.start = timezone.make_aware(datetime(2035, 3, 18, 8))
        self.mine = self.shift('mine', self.me)
        self.theirs = self.shift('theirs', self.other)
        self.client.force_login(self.me)
        self.url = reverse('exchange_choices')

    def shift(self, key, owner, **changes):
        fields = dict(external_id=key, resident=owner, starts_at=self.start,
                      ends_at=self.start + timedelta(hours=4), sector='HOSPITAL · Refração')
        fields.update(changes)
        return Shift.objects.create(**fields)

    def options(self, **params):
        return self.client.get(self.url, params)

    def test_authentication_and_method_are_required(self):
        self.client.logout()
        self.assertEqual(self.options(side='offered').status_code, 302)
        self.client.force_login(self.me)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.assertEqual(self.options(side='bad').status_code, 400)

    def test_sides_are_scoped_and_only_expose_display_fields(self):
        own = self.options(side='offered')
        other = self.options(side='requested')
        self.assertEqual([i['id'] for i in own.json()['options']], [str(self.mine.pk)])
        self.assertEqual([i['id'] for i in other.json()['options']], [str(self.theirs.pk)])
        self.assertEqual(set(other.json()['options'][0]), {'id', 'label', 'category', 'group'})
        self.assertIn('Refração · HOSPITAL', own.json()['options'][0]['label'])
        self.assertIn('no-store', own['Cache-Control'])

    def test_inactive_past_reconciliation_disabled_owner_and_unknown_groups_are_excluded(self):
        self.shift('inactive', self.other, active=False)
        self.shift('historical', self.other, starts_at=self.start-timedelta(days=10000), ends_at=self.start-timedelta(days=9999))
        self.shift('held', self.other, requires_reconciliation=True)
        self.shift('unknown-year', self.other, category='night', resident_group='')
        self.third.is_active=False;self.third.save(update_fields=['is_active'])
        self.shift('disabled', self.third)
        self.assertEqual([i['id'] for i in self.options(side='requested').json()['options']], [str(self.theirs.pk)])

    def test_both_anchor_sides_enforce_opposite_owner(self):
        self.assertEqual(self.options(side='requested', anchor=self.theirs.pk).status_code, 404)
        self.assertEqual(self.options(side='offered', anchor=self.mine.pk).status_code, 404)
        self.assertEqual(self.options(side='requested', anchor=self.mine.pk).status_code, 200)
        self.assertEqual(self.options(side='offered', anchor=self.theirs.pk).status_code, 200)
        self.assertEqual(self.options(side='offered', anchor='999999999999999999999999').status_code, 404)
        self.assertEqual(self.options(side='offered', selected='abc').status_code, 404)

    def test_groups_and_category_filter_options(self):
        self.mine.category='night';self.mine.resident_group='R2';self.mine.save()
        r1=self.shift('night-r1',self.other,category='night',resident_group='R1')
        r3=self.shift('night-r3',self.other,category='night',resident_group='R3')
        self.shift('cinderela-r3',self.other,category='cinderela',resident_group='R3')
        payload=self.options(side='requested',anchor=self.mine.pk).json()
        self.assertEqual([i['id'] for i in payload['options']], [str(r3.pk)])
        self.mine.resident_group='R1';self.mine.save()
        payload=self.options(side='requested',anchor=self.mine.pk).json()
        self.assertEqual([i['id'] for i in payload['options']], [str(r1.pk)])

    def test_results_bounded_and_distant_shift_can_be_found(self):
        rows=[Shift(external_id=f'choice-many-{i}',resident=self.other,starts_at=self.start+timedelta(days=i+1),ends_at=self.start+timedelta(days=i+1,hours=4),sector='Rotina') for i in range(75)]
        Shift.objects.bulk_create(rows)
        response=self.options(side='requested').json()
        self.assertEqual(len(response['options']),50)
        self.assertTrue(response['has_more'])
        target=rows[-1]
        response=self.options(side='requested',q=timezone.localdate(target.starts_at).strftime('%d/%m/%Y')).json()
        self.assertEqual([i['id'] for i in response['options']],[str(target.pk)])
        response=self.options(side='requested',q='2035-03-18').json()
        self.assertEqual([i['id'] for i in response['options']],[str(self.theirs.pk)])

    def test_selected_valid_shift_outside_first_page_is_retained_but_not_wrong_owner(self):
        Shift.objects.bulk_create([Shift(external_id=f'selected-many-{i}',resident=self.other,starts_at=self.start,ends_at=self.start+timedelta(hours=4),sector='Rotina') for i in range(60)])
        selected=self.shift('last-choice',self.other,starts_at=self.start+timedelta(days=500),ends_at=self.start+timedelta(days=500,hours=4))
        payload=self.options(side='requested',selected=selected.pk,q='not matching').json()
        self.assertEqual(payload['selected'],str(selected.pk))
        self.assertEqual(payload['options'][0]['id'],str(selected.pk))
        payload=self.options(side='requested',selected=self.mine.pk).json()
        self.assertEqual(payload['selected'],'')
        self.assertNotIn(str(self.mine.pk),[i['id'] for i in payload['options']])

    def test_accent_insensitive_name_sector_time_and_partial_date_search(self):
        for search in ['jose avila refracao','18/03','08:00','José Ávila']:
            with self.subTest(search=search):
                payload=self.options(side='requested',q=search).json()
                self.assertEqual([i['id'] for i in payload['options']],[str(self.theirs.pk)])
        self.assertEqual(self.options(side='requested',q='31/02/2035').json()['options'],[])
        self.assertEqual(self.options(side='requested',q='9999-12-31').json()['options'],[])

    def test_no_n_plus_one_and_limit_applies_in_database(self):
        Shift.objects.bulk_create([Shift(external_id=f'bounded-{i}',resident=self.other,starts_at=self.start,ends_at=self.start+timedelta(hours=4),sector='Rotina') for i in range(80)])
        with CaptureQueriesContext(connection) as captured:
            response=self.options(side='requested')
        self.assertEqual(len(response.json()['options']),50)
        selects=[q['sql'] for q in captured if 'FROM "schedule_shift"' in q['sql']]
        self.assertEqual(len(selects),1)
        self.assertIn('LIMIT 51',selects[0])

    def test_labels_are_json_text_and_original_data_does_not_change(self):
        self.theirs.sector='HOSPITAL · <script>alert(1)</script>';self.theirs.save()
        payload=self.options(side='requested').json()
        self.assertIn('<script>',payload['options'][0]['label'])
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.sector,'HOSPITAL · <script>alert(1)</script>')
