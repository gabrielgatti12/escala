from datetime import timedelta, timezone as utc
import hashlib

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .calendar_feeds import SESSION_KEY, serialize_calendar
from .models import CalendarSubscription, ResidentAccess, SharedActivity, Shift


class CalendarSubscriptionTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.resident = users.objects.create_user('calendar-source', first_name='Calendar Resident')
        self.account = users.objects.create_user('calendar@example.test', password='Calendar-password-843!')
        ResidentAccess.objects.create(account=self.account, resident=self.resident)
        self.other = users.objects.create_user('calendar-other', password='Other-calendar-password-462!')
        self.start = timezone.now().replace(microsecond=0) + timedelta(days=2)
        self.shift = Shift.objects.create(
            external_id='feed-shift', resident=self.resident, starts_at=self.start,
            ends_at=self.start + timedelta(hours=4), sector='Ambulatório Geral',
        )
        Shift.objects.create(
            external_id='other-feed-shift', resident=self.other, starts_at=self.start,
            ends_at=self.start + timedelta(hours=4), sector='Other private activity',
        )
        self.client.force_login(self.account)

    def create_subscription(self, client=None):
        client = client or self.client
        response = client.post(reverse('calendar_feed_create'))
        self.assertRedirects(response, reverse('calendar_connections'))
        return client.session[SESSION_KEY]['token']

    def test_get_does_not_create_or_rotate_a_link_and_post_requires_login(self):
        page = self.client.get(reverse('calendar_connections'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Gerar meu link privado')
        self.assertFalse(CalendarSubscription.objects.exists())
        self.assertEqual(self.client.get(reverse('calendar_feed_create')).status_code, 405)
        self.assertEqual(self.client.get(reverse('calendar_feed_revoke')).status_code, 405)
        anonymous = Client()
        self.assertEqual(anonymous.post(reverse('calendar_feed_create')).status_code, 302)
        self.assertFalse(CalendarSubscription.objects.exists())
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.account)
        self.assertEqual(csrf_client.post(reverse('calendar_feed_create')).status_code, 403)

    def test_link_is_stored_as_digest_and_only_available_in_its_own_session(self):
        token = self.create_subscription()
        subscription = CalendarSubscription.objects.get(resident=self.resident)
        self.assertEqual(subscription.account, self.account)
        self.assertEqual(len(token), 64)
        self.assertEqual(subscription.token_digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(subscription.token_digest, token)
        page = self.client.get(reverse('calendar_connections'))
        self.assertContains(page, reverse('calendar_feed', args=[token]))
        self.assertIn('private', page['Cache-Control'])
        self.assertIn('no-store', page['Cache-Control'])
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        fresh_session = Client()
        fresh_session.force_login(self.account)
        page = fresh_session.get(reverse('calendar_connections'))
        self.assertNotContains(page, token)
        self.assertContains(page, 'Você já tem uma assinatura ativa')
        self.assertEqual(CalendarSubscription.objects.get().token_digest, subscription.token_digest)

    def test_management_forms_preserve_origin_and_require_a_valid_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.account)
        page = client.get(reverse('calendar_connections'), secure=True)
        self.assertEqual(page['Referrer-Policy'], 'same-origin')
        csrf_token = client.cookies['csrftoken'].value
        response = client.post(
            reverse('calendar_feed_create'), {'csrfmiddlewaretoken': csrf_token},
            secure=True, HTTP_ORIGIN='https://testserver',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        subscription = CalendarSubscription.objects.get(resident=self.resident)
        digest = subscription.token_digest
        # The UI must send the real same-origin value; "null" is never trusted.
        blocked = client.post(
            reverse('calendar_feed_create'), {'csrfmiddlewaretoken': csrf_token},
            secure=True, HTTP_ORIGIN='null',
        )
        self.assertEqual(blocked.status_code, 403)
        subscription.refresh_from_db()
        self.assertEqual(subscription.token_digest, digest)
        response = client.post(
            reverse('calendar_feed_revoke'), {'csrfmiddlewaretoken': csrf_token},
            secure=True, HTTP_ORIGIN='https://testserver',
        )
        self.assertEqual(response.status_code, 302)
        subscription.refresh_from_db()
        self.assertFalse(subscription.enabled)

    def test_feed_is_public_but_only_includes_active_confirmed_own_shifts(self):
        inactive = Shift.objects.create(
            external_id='inactive-feed-shift', resident=self.resident, starts_at=self.start,
            ends_at=self.start + timedelta(hours=3), sector='Inactive private activity', active=False,
        )
        shared = SharedActivity.objects.create(
            external_id='shared-feed-shift', source='test', starts_at=self.start,
            ends_at=self.start + timedelta(hours=2), sector='Unresolved activity',
        )
        shared.residents.set([self.resident, self.other])
        token = self.create_subscription()
        response = Client().get(reverse('calendar_feed', args=[token]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/calendar; charset=utf-8')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertContains(response, 'SUMMARY:Ambulatório Geral')
        self.assertNotContains(response, 'Other private activity')
        self.assertNotContains(response, inactive.sector)
        self.assertNotContains(response, shared.sector)
        self.assertNotContains(response, self.account.username)
        self.assertContains(response, f'DTSTART:{self.start.astimezone(utc.utc):%Y%m%dT%H%M%SZ}')

    def test_new_link_invalidates_previous_and_revocation_is_idempotent(self):
        first = self.create_subscription()
        second = self.create_subscription()
        self.assertNotEqual(first, second)
        self.assertEqual(Client().get(reverse('calendar_feed', args=[first])).status_code, 404)
        self.assertEqual(Client().get(reverse('calendar_feed', args=[second])).status_code, 200)
        self.assertEqual(CalendarSubscription.objects.count(), 1)
        for _ in range(2):
            self.assertRedirects(self.client.post(reverse('calendar_feed_revoke')), reverse('calendar_connections'))
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(Client().get(reverse('calendar_feed', args=[second])).status_code, 404)
        self.assertFalse(CalendarSubscription.objects.get().enabled)

    def test_other_account_cannot_view_or_revoke_residents_link(self):
        token = self.create_subscription()
        other_client = Client()
        other_client.force_login(self.other)
        self.assertNotContains(other_client.get(reverse('calendar_connections')), token)
        self.assertEqual(other_client.post(reverse('calendar_feed_revoke'), {
            'resident': self.resident.pk, 'token': token,
        }).status_code, 302)
        self.assertTrue(CalendarSubscription.objects.get(resident=self.resident).enabled)
        response = other_client.post(reverse('calendar_feed_create'), {'resident': self.resident.pk})
        self.assertRedirects(response, reverse('calendar_connections'))
        other_token = other_client.session[SESSION_KEY]['token']
        self.assertNotEqual(token, other_token)
        self.assertEqual(CalendarSubscription.objects.count(), 2)
        self.assertTrue(CalendarSubscription.objects.filter(resident=self.other).exists())

    def test_feed_updates_with_assignment_changes_without_changing_uid(self):
        token = self.create_subscription()
        url = reverse('calendar_feed', args=[token])
        before = Client().get(url)
        uid = f'UID:shift-{self.shift.pk}@escala'
        self.assertContains(before, uid)
        self.shift.starts_at += timedelta(days=1)
        self.shift.ends_at += timedelta(days=1)
        self.shift.sector = 'Updated sector'
        self.shift.save()
        after = Client().get(url)
        self.assertContains(after, uid)
        self.assertContains(after, 'SUMMARY:Updated sector')
        self.shift.resident = self.other
        self.shift.save(update_fields=['resident'])
        self.assertNotContains(Client().get(url), uid)

    def test_disabled_resident_or_login_stops_public_feed(self):
        token = self.create_subscription()
        url = reverse('calendar_feed', args=[token])
        self.account.is_active = False
        self.account.save(update_fields=['is_active'])
        self.assertEqual(Client().get(url).status_code, 404)
        self.account.is_active = True
        self.account.save(update_fields=['is_active'])
        self.resident.is_active = False
        self.resident.save(update_fields=['is_active'])
        self.assertEqual(Client().get(url).status_code, 404)
        self.assertRedirects(
            self.client.post(reverse('calendar_feed_create')),
            '/accounts/login/?next=/calendario/conectar/gerar/',
        )

    def test_admin_without_resident_link_gets_explanation_and_cannot_generate(self):
        admin = get_user_model().objects.create_superuser('calendar-admin', password='Admin-calendar-password-932!')
        self.client.force_login(admin)
        page = self.client.get(reverse('calendar_connections'))
        self.assertContains(page, 'Identifique seu residente primeiro')
        self.assertNotContains(page, 'Gerar meu link privado')
        self.assertEqual(self.client.post(reverse('calendar_feed_create')).status_code, 403)
        self.assertFalse(CalendarSubscription.objects.exists())

    def test_changed_mapping_cannot_expose_token_retained_in_session(self):
        token = self.create_subscription()
        ResidentAccess.objects.filter(account=self.account).update(resident=self.other)
        page = self.client.get(reverse('calendar_connections'))
        self.assertNotContains(page, token)
        self.assertIsNone(page.context['feed_url'])

    def test_remapping_and_a_replacement_login_do_not_revive_previous_link(self):
        token = self.create_subscription()
        previous_url = reverse('calendar_feed', args=[token])
        stored = self.client.session[SESSION_KEY]
        ResidentAccess.objects.filter(account=self.account).update(resident=self.other)
        replacement = get_user_model().objects.create_user(
            'replacement@example.test', password='Replacement-calendar-password-736!',
        )
        ResidentAccess.objects.create(account=replacement, resident=self.resident)
        replacement_client = Client()
        replacement_client.force_login(replacement)
        # Even a token remaining in the browser session belongs to its creator.
        session = replacement_client.session
        session[SESSION_KEY] = stored
        session.save()
        page = replacement_client.get(reverse('calendar_connections'))
        self.assertNotContains(page, token)
        self.assertFalse(page.context['subscription_enabled'])
        self.assertEqual(Client().get(previous_url).status_code, 404)

        replacement_token = self.create_subscription(replacement_client)

        self.assertEqual(CalendarSubscription.objects.get(resident=self.resident).account, replacement)
        self.assertEqual(Client().get(previous_url).status_code, 404)
        self.assertEqual(Client().get(reverse('calendar_feed', args=[replacement_token])).status_code, 200)

    def test_deleted_creator_removes_subscription_and_denies_old_link(self):
        token = self.create_subscription()
        self.account.delete()
        self.assertFalse(CalendarSubscription.objects.exists())
        self.assertEqual(Client().get(reverse('calendar_feed', args=[token])).status_code, 404)

    def test_legacy_subscription_without_creator_cannot_serve_or_reveal_token(self):
        token = self.create_subscription()
        CalendarSubscription.objects.filter(resident=self.resident).update(account=None)
        page = self.client.get(reverse('calendar_connections'))
        self.assertNotContains(page, token)
        self.assertEqual(Client().get(reverse('calendar_feed', args=[token])).status_code, 404)

    def test_invalid_token_is_plain_404_and_feed_disallows_writes(self):
        for token in ['missing', 'A' * 64, 'á' * 64]:
            response = Client().get(reverse('calendar_feed', args=[token]))
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
            self.assertEqual(response['Cache-Control'], 'private, no-store')
        token = self.create_subscription()
        self.assertEqual(Client().post(reverse('calendar_feed', args=[token])).status_code, 405)

    def test_ics_escapes_injection_and_folds_long_unicode_lines_by_octets(self):
        self.shift.sector = 'Patologia; vírgula, barra\\\r\nEND:VEVENT ' + 'á' * 100 + '\x00\x7f'
        output = serialize_calendar([self.shift])
        unfolded = output.replace('\r\n ', '')
        self.assertIn('Patologia\\; vírgula\\, barra\\\\\\nEND:VEVENT ', unfolded)
        self.assertEqual(output.count('BEGIN:VEVENT\r\n'), 1)
        self.assertEqual(output.count('\r\nEND:VEVENT\r\n'), 1)
        self.assertNotIn('\x00', output)
        self.assertNotIn('\x7f', output)
        self.assertTrue(all(len(line.encode('utf-8')) <= 75 for line in output.split('\r\n')))
        self.assertTrue(output.endswith('END:VCALENDAR\r\n'))

    def test_calendar_views_download_and_subscription_use_activity_before_location(self):
        token = self.create_subscription()
        feed_url = reverse('calendar_feed', args=[token])
        date = timezone.localdate(self.start)
        original_id = self.shift.external_id
        for raw, displayed in [
            ('HOSPITAL · PA (Pronto Atendimento)', 'PA (Pronto Atendimento) · HOSPITAL'),
            ('INSTITUTO\u00a0·\u00a0Refração', 'Refração · INSTITUTO'),
        ]:
            with self.subTest(label=raw):
                self.shift.sector = raw
                self.shift.save(update_fields=['sector'])
                for path, params in [
                    ('/', {'week': date.isoformat()}),
                    ('/', {'week': date.isoformat(), 'layout': 'sector'}),
                    (reverse('calendar'), {'month': date.strftime('%Y-%m')}),
                ]:
                    self.assertContains(self.client.get(path, params), f'<h3>{displayed}</h3>', html=True)
                for response in [self.client.get(reverse('calendar_ics')), Client().get(feed_url)]:
                    self.assertEqual(response.status_code, 200)
                    text = response.content.decode().replace('\r\n ', '')
                    self.assertIn(f'SUMMARY:{displayed}\r\n', text)
                    self.assertIn(f'UID:shift-{self.shift.pk}@escala\r\n', text)
                self.shift.refresh_from_db()
                self.assertEqual(self.shift.sector, raw)
                self.assertEqual(self.shift.external_id, original_id)

    def test_monthly_duty_names_are_not_reversed_in_calendar_export(self):
        self.shift.sector = 'Cinderela · R1'
        text = serialize_calendar([self.shift]).replace('\r\n ', '')
        self.assertIn('SUMMARY:Cinderela · R1\r\n', text)
