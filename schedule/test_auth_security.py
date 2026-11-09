from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .auth import EmailOrUsernameBackend
from .identity import is_account_enabled
from .models import Exchange, LoginAttempt, ResidentAccess, Shift


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class IdentityAccessSecurityTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.resident = users.objects.create_user('security-source', first_name='Pessoa Fictícia')
        self.alias = users.objects.create_user('security.alias@example.test', password='Fictitious-login-password-294!')
        ResidentAccess.objects.create(account=self.alias, resident=self.resident)
        start = timezone.now() + timedelta(days=3)
        Shift.objects.create(
            external_id='security-source-shift', resident=self.resident,
            starts_at=start, ends_at=start + timedelta(hours=4), sector='Setor de teste',
        )

    def activation_url(self, user):
        return reverse('activate_account', kwargs={
            'uidb64': urlsafe_base64_encode(force_bytes(user.pk)),
            'token': default_token_generator.make_token(user),
        })

    def test_active_alias_still_authenticates_for_active_resident(self):
        self.assertTrue(is_account_enabled(self.alias))
        self.assertTrue(self.client.login(username=self.alias.username, password='Fictitious-login-password-294!'))
        self.assertEqual(self.client.get('/').context['resident_user'], self.resident)

    def test_disabled_resident_blocks_email_and_legacy_username_authentication(self):
        self.resident.is_active = False
        self.resident.save(update_fields=['is_active'])
        self.assertFalse(is_account_enabled(self.alias))
        self.assertFalse(self.client.login(username=self.alias.username, password='Fictitious-login-password-294!'))
        self.alias.username = 'security-legacy-alias'
        self.alias.save(update_fields=['username'])
        self.assertFalse(self.client.login(username=self.alias.username, password='Fictitious-login-password-294!'))
        self.assertIsNone(EmailOrUsernameBackend().get_user(self.alias.pk))

    def test_existing_custom_backend_session_stops_when_resident_is_disabled(self):
        self.client.force_login(self.alias, backend='schedule.auth.EmailOrUsernameBackend')
        self.resident.is_active = False
        self.resident.save(update_fields=['is_active'])
        self.assertRedirects(self.client.get('/'), '/accounts/login/?next=/')

    def test_existing_legacy_backend_session_stops_when_resident_is_disabled(self):
        self.client.force_login(self.alias, backend='django.contrib.auth.backends.ModelBackend')
        self.resident.is_active = False
        self.resident.save(update_fields=['is_active'])
        self.assertRedirects(self.client.get('/'), '/accounts/login/?next=/')

    def test_disabled_legacy_session_cannot_submit_a_trade(self):
        other = get_user_model().objects.create_user('security-other')
        offered = Shift.objects.get(resident=self.resident)
        requested = Shift.objects.create(
            external_id='security-other-shift', resident=other,
            starts_at=offered.starts_at + timedelta(days=1),
            ends_at=offered.ends_at + timedelta(days=1), sector='Setor de teste',
        )
        self.client.force_login(self.alias, backend='django.contrib.auth.backends.ModelBackend')
        self.resident.is_active = False
        self.resident.save(update_fields=['is_active'])
        response = self.client.post(reverse('propose'), {'offered': offered.pk, 'requested': requested.pk})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Exchange.objects.exists())

    def test_source_password_does_not_bypass_separate_alias(self):
        self.resident.set_password('Fictitious-source-password-491!')
        self.resident.save(update_fields=['password'])
        self.assertFalse(is_account_enabled(self.resident))
        self.assertFalse(self.client.login(username=self.resident.username, password='Fictitious-source-password-491!'))
        self.assertIsNone(EmailOrUsernameBackend().get_user(self.resident.pk))
        self.assertTrue(self.client.login(username=self.alias.username, password='Fictitious-login-password-294!'))

    def test_legacy_source_session_cannot_bypass_separate_alias(self):
        self.resident.set_password('Fictitious-source-password-491!')
        self.resident.save(update_fields=['password'])
        self.client.force_login(self.resident, backend='django.contrib.auth.backends.ModelBackend')
        self.assertRedirects(self.client.get('/'), '/accounts/login/?next=/')

    def test_source_identity_cannot_receive_or_use_first_access_link(self):
        admin = get_user_model().objects.create_superuser('security-admin', password='Fictitious-admin-password-654!')
        self.client.force_login(admin)
        response = self.client.post(reverse('account_activation_link', args=[self.resident.pk]))
        self.assertRedirects(response, '/?tab=accounts')
        self.assertContains(Client().get(self.activation_url(self.resident)), 'Link indisponível')
        self.resident.refresh_from_db()
        self.assertFalse(self.resident.has_usable_password())

    def test_pending_alias_can_activate_but_disabled_resident_cannot(self):
        self.alias.set_unusable_password()
        self.alias.save(update_fields=['password'])
        self.assertTrue(is_account_enabled(self.alias))
        url = self.activation_url(self.alias)
        visitor = Client()
        self.assertEqual(visitor.get(url).status_code, 302)
        self.resident.is_active = False
        self.resident.save(update_fields=['is_active'])
        self.assertContains(Client().get(url), 'Link indisponível')

    def test_common_unmapped_login_and_admin_remain_enabled(self):
        users = get_user_model()
        for user in [
            users.objects.create_user('security-unmapped', password='Fictitious-person-password-425!'),
            users.objects.create_superuser('security-unmapped-admin', password='Fictitious-admin-password-654!'),
        ]:
            self.assertTrue(is_account_enabled(user))

    def test_cross_site_forms_cannot_exhaust_login_limits_for_victims_browser(self):
        visitor = Client(enforce_csrf_checks=True)
        address = '203.0.113.42'
        for attempt in range(30):
            response = visitor.post(
                reverse('login'),
                {'username': f'security-crosssite-{attempt}@example.test', 'password': 'wrong'},
                secure=True, HTTP_ORIGIN='https://outside.example.test', REMOTE_ADDR=address,
            )
            self.assertEqual(response.status_code, 403)
        self.assertFalse(LoginAttempt.objects.exists())

        visitor.get(reverse('login'), secure=True, REMOTE_ADDR=address)
        csrf_token = visitor.cookies['csrftoken'].value
        response = visitor.post(
            reverse('login'),
            {'username': self.alias.username, 'password': 'Fictitious-login-password-294!', 'csrfmiddlewaretoken': csrf_token},
            secure=True, HTTP_ORIGIN='https://testserver', REMOTE_ADDR=address,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(visitor.session['_auth_user_id']), self.alias.pk)
