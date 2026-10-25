from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from .contacts import apply_contacts, contact_preview
from .models import ResidentAccess, Shift


class ContactAccountRegressionTests(TestCase):
    def setUp(self):
        self.password = 'Resident-existing-password-932!'
        self.resident = get_user_model().objects.create_user(
            'source-resident', first_name='Source Resident', password=self.password,
        )
        start = timezone.now() + timedelta(days=1)
        Shift.objects.create(
            external_id='account-regression-shift', resident=self.resident,
            starts_at=start, ends_at=start + timedelta(hours=4), sector='Hospital',
        )

    def test_alias_preserves_password_and_revokes_source_login_and_session(self):
        legacy = Client()
        self.assertTrue(legacy.login(username=self.resident.username, password=self.password))

        account = apply_contacts('Source Resident,source@example.test', {0})[0]

        self.assertTrue(account.check_password(self.password))
        self.resident.refresh_from_db()
        self.assertFalse(self.resident.has_usable_password())
        self.assertFalse(self.client.login(username=self.resident.username, password=self.password))
        self.assertTrue(self.client.login(username='SOURCE@EXAMPLE.TEST', password=self.password))
        self.assertEqual(self.client.get('/').context['resident_user'], self.resident)
        self.assertRedirects(legacy.get('/'), '/accounts/login/?next=/')

    def test_existing_alias_keeps_own_password_and_revokes_old_source_access(self):
        account = get_user_model().objects.create_user(
            'existing@example.test', password='Alias-existing-password-739!',
        )
        ResidentAccess.objects.create(account=account, resident=self.resident)
        legacy = Client()
        self.assertTrue(legacy.login(username=self.resident.username, password=self.password))

        apply_contacts('Source Resident,updated@example.test', {0})

        account.refresh_from_db()
        self.assertTrue(account.check_password('Alias-existing-password-739!'))
        self.assertTrue(self.client.login(username='updated@example.test', password='Alias-existing-password-739!'))
        self.assertFalse(self.client.login(username=self.resident.username, password=self.password))
        self.assertRedirects(legacy.get('/'), '/accounts/login/?next=/')

    def test_mapped_admin_keeps_privileges_password_and_session(self):
        admin = get_user_model().objects.create_superuser(
            'account-admin', password='Admin-existing-password-845!',
        )
        ResidentAccess.objects.create(account=admin, resident=self.resident)
        self.client.force_login(admin, backend='django.contrib.auth.backends.ModelBackend')

        apply_contacts('Source Resident,admin@example.test', {0})

        admin.refresh_from_db()
        self.assertEqual(admin.username, 'admin@example.test')
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password('Admin-existing-password-845!'))
        self.assertEqual(self.client.get('/?tab=accounts').status_code, 200)
        self.resident.refresh_from_db()
        self.assertFalse(self.resident.has_usable_password())

    def test_disabled_mapped_account_is_not_imported_or_reactivated(self):
        account = get_user_model().objects.create_user('disabled@example.test', is_active=False)
        ResidentAccess.objects.create(account=account, resident=self.resident)

        row = contact_preview('Source Resident,new@example.test')[0]

        self.assertFalse(row['ready'])
        self.assertIn('Conta de acesso desativada', row['status'])
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            apply_contacts('Source Resident,new@example.test', {0})
        account.refresh_from_db()
        self.assertFalse(account.is_active)
        self.assertEqual(account.username, 'disabled@example.test')
