from datetime import timedelta
from io import StringIO
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.utils import timezone
from .models import CalendarSubscription, ResidentAccess, Shift


class SeparateLoginTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.source = users.objects.create_user('imported-person', first_name='Pessoa Fictícia')
        self.admin = users.objects.create_superuser('personal@example.test', email='personal@example.test', password='Existing-admin-password-624!')
        ResidentAccess.objects.create(account=self.admin, resident=self.source)
        start = timezone.now()+timedelta(days=5)
        self.shift = Shift.objects.create(external_id='split-test', resident=self.source, starts_at=start, ends_at=start+timedelta(hours=4), sector='Teste')

    def split(self, **kwargs):
        call_command('separate_resident_login', resident_name='Pessoa Fictícia', admin_email='operator@example.test', resident_email='personal@example.test', stdout=StringIO(), **kwargs)

    def test_split_preserves_admin_password_session_and_resident_schedule(self):
        client = Client();client.force_login(self.admin)
        CalendarSubscription.objects.create(resident=self.source, account=self.admin, token_digest='0'*64)
        self.split()
        self.admin.refresh_from_db();self.shift.refresh_from_db()
        self.assertEqual(self.admin.username, 'operator@example.test')
        self.assertTrue(self.admin.check_password('Existing-admin-password-624!'))
        self.assertTrue(self.admin.is_superuser)
        self.assertEqual(client.get('/?tab=accounts').status_code, 200)
        personal = get_user_model().objects.get(username='personal@example.test')
        self.assertFalse(personal.is_superuser);self.assertFalse(personal.is_staff)
        self.assertFalse(personal.has_usable_password())
        self.assertEqual(ResidentAccess.objects.get(resident=self.source).account, personal)
        self.assertFalse(ResidentAccess.objects.filter(account=self.admin).exists())
        self.assertEqual(self.shift.resident_id, self.source.pk)
        self.assertFalse(CalendarSubscription.objects.get().enabled)

    def test_repeating_command_preserves_personal_password_and_restricts_admin_routes(self):
        self.split()
        personal = get_user_model().objects.get(username='personal@example.test')
        personal.set_password('Existing-personal-password-157!');personal.save()
        self.split()
        personal.refresh_from_db()
        self.assertTrue(personal.check_password('Existing-personal-password-157!'))
        client = Client();self.assertTrue(client.login(username='PERSONAL@EXAMPLE.TEST', password='Existing-personal-password-157!'))
        self.assertEqual(client.get('/').context['resident_user'], self.source)
        self.assertEqual(client.get('/?tab=accounts').status_code, 403)
        self.assertEqual(get_user_model().objects.count(), 3)

    def test_conflicting_email_does_not_modify_accounts_or_mapping(self):
        other = get_user_model().objects.create_user('operator@example.test')
        with self.assertRaises(CommandError):
            self.split()
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.username, 'personal@example.test')
        self.assertEqual(ResidentAccess.objects.get().account_id, self.admin.pk)

    def test_ambiguous_name_does_not_choose_a_resident(self):
        duplicate = get_user_model().objects.create_user('other-import', first_name='Pessoa Fictícia')
        Shift.objects.create(external_id='other-split', resident=duplicate, starts_at=self.shift.starts_at, ends_at=self.shift.ends_at, sector='Outro')
        with self.assertRaises(CommandError):
            self.split()
        self.assertEqual(ResidentAccess.objects.get().account_id, self.admin.pk)
