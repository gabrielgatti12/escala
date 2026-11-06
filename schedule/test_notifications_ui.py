from datetime import timedelta
from io import StringIO
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .live import data_revision
from .models import Notification, ResidentAccess, SyncHeartbeat


class NotificationInterfaceTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.resident = users.objects.create_user('notice-source')
        self.account = users.objects.create_user('notice@example.test', password='Private-test-password-628!')
        ResidentAccess.objects.create(account=self.account, resident=self.resident)
        self.other = users.objects.create_user('other-notice-source')
        self.notice = Notification.objects.create(resident=self.resident, kind='request', title='Minha solicitação', message='Confira a proposta.', link='/?tab=requests&box=received')
        self.secret = Notification.objects.create(resident=self.other, kind='schedule', title='Aviso reservado de outra pessoa', message='Conteúdo privado.')
        self.client.force_login(self.account)

    def test_alias_only_sees_own_resident_notifications(self):
        response = self.client.get(reverse('notifications'))
        self.assertContains(response, self.notice.title)
        self.assertNotContains(response, self.secret.title)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(self.client.get(reverse('notification_detail', args=[self.secret.pk])).status_code, 404)
        self.notice.refresh_from_db()
        self.assertIsNone(self.notice.read_at)

    def test_notification_reads_require_post_csrf_and_ownership(self):
        url = reverse('notification_read', args=[self.notice.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(reverse('notification_read', args=[self.secret.pk])).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.account)
        self.assertEqual(csrf.post(url).status_code, 403)
        self.assertRedirects(self.client.post(url, {'resident':self.other.pk}), reverse('notifications'))
        self.notice.refresh_from_db();self.secret.refresh_from_db()
        self.assertIsNotNone(self.notice.read_at)
        self.assertIsNone(self.secret.read_at)

    def test_read_all_does_not_change_other_residents_notifications(self):
        self.client.post(reverse('notifications_read_all'))
        self.assertFalse(Notification.objects.filter(resident=self.resident, read_at__isnull=True).exists())
        self.secret.refresh_from_db()
        self.assertIsNone(self.secret.read_at)
        self.assertNotContains(self.client.get(reverse('notifications')+'?unread=1'), self.notice.title)

    def test_detail_renders_before_after_local_times_and_escapes_content(self):
        day = timezone.localdate()+timedelta(days=1)
        self.notice.kind = 'schedule'
        self.notice.payload = {'changes':[{
            'action':'updated',
            'before':{'kind':'shift','date':day.isoformat(),'starts_at':f'{day}T08:00:00-03:00','ends_at':f'{day}T12:00:00-03:00','sector':'<script>private</script>','status':'confirmed'},
            'after':{'kind':'shift','date':day.isoformat(),'starts_at':f'{day}T13:00:00-03:00','ends_at':f'{day}T18:00:00-03:00','sector':'Patologia','status':'confirmed'},
        }]}
        self.notice.save()
        response = self.client.get(reverse('notification_detail', args=[self.notice.pk]))
        self.assertContains(response, 'Antes')
        self.assertContains(response, 'Agora')
        self.assertContains(response, '08:00–12:00')
        self.assertContains(response, '13:00–18:00')
        self.assertContains(response, '&lt;script&gt;private&lt;/script&gt;')
        self.assertNotContains(response, '<script>private</script>')

    def test_pagination_and_type_filter_preserve_private_scope(self):
        Notification.objects.bulk_create([Notification(resident=self.resident, kind='reminder', title=f'Lembrete {i}', message='Teste') for i in range(24)])
        response = self.client.get(reverse('notifications')+'?kind=reminder&page=2')
        self.assertEqual(len(response.context['notifications']), 4)
        self.assertContains(response, 'kind=reminder')
        self.assertNotContains(response, self.secret.title)

    def test_badge_and_live_revision_use_current_resident_only(self):
        own = data_revision(self.resident)
        other = data_revision(self.other)
        self.client.post(reverse('notification_read', args=[self.notice.pk]))
        self.assertNotEqual(data_revision(self.resident), own)
        self.assertEqual(data_revision(self.other), other)
        response = self.client.get('/dados/status/')
        self.assertEqual(response.json()['revision'], data_revision(self.resident))
        self.assertNotIn(self.notice.message, response.content.decode())

    def test_anonymous_users_cannot_read_notices(self):
        self.client.logout()
        for url in [reverse('notifications'), reverse('notification_detail', args=[self.notice.pk])]:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_renewed_reminder_uses_latest_time_and_returns_to_top(self):
        now = timezone.now().replace(microsecond=0)
        Notification.objects.filter(pk=self.notice.pk).update(created_at=now-timedelta(days=1))
        reminder = Notification.objects.create(resident=self.resident, kind='reminder', title='Lembrete renovado', message='Ainda aguarda resposta.', payload={'last_reminded_at':now.isoformat()})
        Notification.objects.filter(pk=reminder.pk).update(created_at=now-timedelta(days=7))
        response = self.client.get(reverse('notifications'))
        self.assertEqual(response.context['notifications'][0].pk, reminder.pk)
        reminder.refresh_from_db()
        self.assertEqual(reminder.display_at, now)


class ManualSyncInterfaceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('sync-operator')
        self.client.force_login(self.user)

    def test_only_admin_can_queue_manual_refresh_without_accessing_google(self):
        url = reverse('request_source_refresh')
        self.assertEqual(self.client.post(url).status_code, 403)
        self.user.is_superuser = self.user.is_staff = True;self.user.save()
        self.assertEqual(self.client.get(url).status_code, 405)
        with patch('schedule.sheets.fetch_workbook') as fetch:
            self.assertRedirects(self.client.post(url), '/?tab=sources')
            first = SyncHeartbeat.objects.get().refresh_requested_at
            self.client.post(url)
            self.assertEqual(SyncHeartbeat.objects.get().refresh_requested_at, first)
            fetch.assert_not_called()
        self.assertContains(self.client.get('/?tab=sources'), 'Atualização solicitada')

    def test_sync_status_command_reports_all_sources_and_default_daily_interval(self):
        stream = StringIO()
        with patch.dict('os.environ', {'SYNC_INTERVAL':'86400'}):
            call_command('sync_status', json=True, stdout=stream)
        data = json.loads(stream.getvalue())
        self.assertEqual(data['status'], 'unknown')
        self.assertEqual(data['interval_label'], '24 horas')
