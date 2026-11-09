from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import SESSION_KEY, get_user_model
from django.db import OperationalError
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from .models import LoginAttempt, ResidentAccess


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class LoginLimitTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('limit@example.test',password='Fictional-login-password-726!')

    def attempt(self, identity=None, password='wrong', address='192.0.2.1', path='/accounts/login/'):
        return self.client.post(path, {'username':identity or self.user.username, 'password':password}, REMOTE_ADDR=address)

    def test_wrong_passwords_trigger_cooldown_including_correct_password_and_case_variants(self):
        for _ in range(5):
            self.assertEqual(self.attempt().status_code,200)
        response=self.attempt(identity='LIMIT@EXAMPLE.TEST',password='Fictional-login-password-726!')
        self.assertEqual(response.status_code,429)
        self.assertGreater(int(response['Retry-After']),0)
        self.assertNotIn(SESSION_KEY,self.client.session)
        self.assertNotIn(self.user.username,response.content.decode())
        self.assertTrue(all('example' not in row.key and '192.0.2' not in row.key for row in LoginAttempt.objects.all()))

    def test_other_address_is_not_locked_by_targeted_failures(self):
        for _ in range(5):self.attempt()
        self.assertEqual(self.attempt(password='Fictional-login-password-726!',address='192.0.2.2').status_code,302)
        self.assertEqual(int(self.client.session[SESSION_KEY]),self.user.pk)

    def test_ip_limit_blocks_rotating_unknown_usernames(self):
        for index in range(30):
            self.assertEqual(self.attempt(identity=f'absent-{index}@example.test').status_code,200)
        self.assertEqual(self.attempt(identity='another@example.test').status_code,429)

    def test_cooldown_expires_and_success_resets_pair_counter(self):
        for _ in range(5):self.attempt()
        later=timezone.now()+timedelta(minutes=16)
        with patch('schedule.security.timezone.now',return_value=later):
            self.assertEqual(self.attempt(password='Fictional-login-password-726!').status_code,302)
        self.client.logout()
        for _ in range(5):self.assertEqual(self.attempt().status_code,200)

    def test_admin_login_uses_same_shared_limits(self):
        for _ in range(5):self.attempt(path='/admin/login/')
        self.assertEqual(self.attempt(path='/accounts/login/').status_code,429)

    @override_settings(SECURE_PROXY_SSL_HEADER=None)
    def test_untrusted_forwarded_header_does_not_bypass_limits(self):
        for index in range(5):
            self.client.post('/accounts/login/',{'username':self.user.username,'password':'wrong'},REMOTE_ADDR='192.0.2.1',HTTP_X_FORWARDED_FOR=f'198.51.100.{index}')
        self.assertEqual(self.attempt().status_code,429)

    def test_counter_database_failure_never_bypasses_the_limit(self):
        with patch('schedule.security.retry_after',side_effect=OperationalError('unavailable')):
            response=self.attempt(password='Fictional-login-password-726!')
        self.assertEqual(response.status_code,503)
        self.assertNotIn(SESSION_KEY,self.client.session)

    def test_authenticated_html_and_logout_are_not_cacheable(self):
        self.client.force_login(self.user)
        for response in [self.client.get('/'),self.client.post('/accounts/logout/')]:
            self.assertIn('no-store',response['Cache-Control'])
            self.assertIn('private',response['Cache-Control'])

    def test_deactivation_flushes_custom_session_so_reactivation_does_not_revive_it(self):
        resident=get_user_model().objects.create_user('revocation-source')
        ResidentAccess.objects.create(account=self.user,resident=resident)
        self.client.force_login(self.user,backend='schedule.auth.EmailOrUsernameBackend')
        resident.is_active=False;resident.save()
        self.assertEqual(self.client.get('/').status_code,302)
        self.assertNotIn(SESSION_KEY,self.client.session)
        resident.is_active=True;resident.save()
        self.assertEqual(self.client.get('/').status_code,302)
