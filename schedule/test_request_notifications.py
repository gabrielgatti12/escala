from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from .models import Allocation, Exchange, Notification, ResidentAccess, SharedActivity, Shift
from .reminders import notify_request_created, refresh_request_notifications
from .services import propose_allocation, propose_exchange, respond_allocation, respond_exchange


class RequestNotificationTests(TestCase):
    def setUp(self):
        self.a = get_user_model().objects.create_user('notice-ana', first_name='Ana')
        self.b = get_user_model().objects.create_user('notice-bia', first_name='Bia')
        self.c = get_user_model().objects.create_user('notice-carla', first_name='Carla')
        self.now = timezone.now() + timedelta(seconds=1)
        start = self.now + timedelta(days=10)
        self.offered = Shift.objects.create(external_id='notice-offered', resident=self.a, starts_at=start, ends_at=start + timedelta(hours=4), sector='Hospital')
        self.requested = Shift.objects.create(external_id='notice-requested', resident=self.b, starts_at=start + timedelta(days=1), ends_at=start + timedelta(days=1, hours=4), sector='Instituto')
        self.mine = SharedActivity.objects.create(external_id='notice-mine', source='notice-test', starts_at=start + timedelta(days=2), ends_at=start + timedelta(days=2, hours=4), sector='Ambulatório')
        self.theirs = SharedActivity.objects.create(external_id='notice-theirs', source='notice-test', starts_at=start + timedelta(days=2), ends_at=start + timedelta(days=2, hours=4), sector='Wet lab')
        self.mine.residents.set([self.a, self.b]); self.theirs.residents.set([self.a, self.b])

    def exchange(self):
        return propose_exchange(self.a, self.offered.pk, self.requested.pk)

    def allocation(self):
        return propose_allocation(self.a, self.mine.pk, self.theirs.pk)

    def test_creation_notifies_only_the_recipient_resident_identity(self):
        account = get_user_model().objects.create_user('bia@example.test')
        admin = get_user_model().objects.create_superuser('notice-admin', password='test-only-password')
        ResidentAccess.objects.create(account=account, resident=self.b)
        exchange = self.exchange()
        allocation = self.allocation()
        notices = Notification.objects.order_by('pk')
        self.assertEqual(notices.count(), 2)
        self.assertEqual(set(notices.values_list('resident_id', flat=True)), {self.b.pk})
        self.assertFalse(Notification.objects.filter(resident_id__in=[account.pk, self.a.pk, self.c.pk, admin.pk]).exists())
        self.assertEqual({notice.payload['request_type'] for notice in notices}, {'exchange', 'allocation'})
        self.assertEqual({notice.payload['request_id'] for notice in notices}, {exchange.pk, allocation.pk})
        self.assertTrue(all(notice.link == '/?tab=requests&box=received' for notice in notices))
        notify_request_created(exchange); notify_request_created(allocation)
        self.assertEqual(Notification.objects.count(), 2)

    def test_daily_reminder_is_idempotent_and_reuses_one_row(self):
        exchange = self.exchange()
        first = refresh_request_notifications(now=self.now + timedelta(hours=23))
        self.assertEqual(first['reminders'], 0)
        due = self.now + timedelta(hours=24)
        self.assertEqual(refresh_request_notifications(now=due)['reminders'], 1)
        reminder = Notification.objects.get(kind='reminder')
        self.assertEqual(reminder.resident, self.b)
        self.assertEqual(reminder.payload['request_id'], exchange.pk)
        self.assertEqual(refresh_request_notifications(now=due + timedelta(minutes=5))['reminders'], 0)
        self.assertEqual(refresh_request_notifications(now=due + timedelta(hours=23, minutes=59))['reminders'], 0)
        reminder.read_at = due + timedelta(minutes=10); reminder.save(update_fields=['read_at'])
        original_created_at = reminder.created_at
        next_due = due + timedelta(hours=24)
        self.assertEqual(refresh_request_notifications(now=next_due)['reminders'], 1)
        reminder.refresh_from_db()
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(reminder.created_at, original_created_at)
        self.assertIsNone(reminder.read_at)
        self.assertEqual(reminder.payload['last_reminded_at'], next_due.isoformat())

    def test_allocation_receives_daily_reminder(self):
        proposal = self.allocation()
        self.assertEqual(refresh_request_notifications(now=self.now + timedelta(hours=24))['reminders'], 1)
        notice = Notification.objects.get(kind='reminder')
        self.assertEqual((notice.payload['request_type'], notice.payload['request_id']), ('allocation', proposal.pk))

    def test_legacy_request_first_receives_only_one_notice_and_waits_24_hours(self):
        proposal = Exchange.objects.create(offered=self.offered, requested=self.requested, proposer=self.a, recipient=self.b)
        Exchange.objects.filter(pk=proposal.pk).update(created_at=self.now - timedelta(days=3))
        self.assertEqual(refresh_request_notifications(now=self.now), {'requests':1, 'reminders':0, 'closed':0})
        self.assertEqual(refresh_request_notifications(now=self.now + timedelta(minutes=5))['reminders'], 0)
        self.assertEqual(refresh_request_notifications(now=self.now + timedelta(hours=24))['reminders'], 1)
        self.assertEqual(Notification.objects.count(), 2)

    def test_responses_close_notices_without_deleting_history(self):
        exchange = self.exchange()
        allocation = self.allocation()
        refresh_request_notifications(now=self.now + timedelta(hours=24))
        self.assertEqual(Notification.objects.count(), 4)
        respond_exchange(self.b, exchange.pk, 'reject')
        respond_allocation(self.a, allocation.pk, 'cancel')
        self.assertFalse(Notification.objects.filter(read_at__isnull=True).exists())
        self.assertEqual(Notification.objects.count(), 4)
        self.assertEqual(refresh_request_notifications(now=self.now + timedelta(days=2))['reminders'], 0)

    def test_accepted_request_never_receives_a_reminder(self):
        exchange = self.exchange()
        respond_exchange(self.b, exchange.pk, 'accept')
        refresh_request_notifications(now=self.now + timedelta(days=2))
        self.assertFalse(Notification.objects.filter(kind='reminder').exists())
        self.assertIsNotNone(Notification.objects.get(kind='request').read_at)

    def test_expired_inactive_or_changed_ownership_requests_do_not_remind(self):
        exchange = self.exchange()
        self.offered.active = False; self.offered.save(update_fields=['active'])
        refresh_request_notifications(now=self.now + timedelta(days=2))
        self.assertFalse(Notification.objects.filter(kind='reminder').exists())
        self.assertIsNotNone(Notification.objects.get(kind='request').read_at)
        self.offered.active = True; self.offered.resident = self.c
        self.offered.save(update_fields=['active', 'resident'])
        refresh_request_notifications(now=self.now + timedelta(days=3))
        self.assertEqual(Notification.objects.count(), 1)
        self.offered.resident = self.a; self.offered.save(update_fields=['resident'])
        refresh_request_notifications(now=self.now + timedelta(days=12))
        self.assertFalse(Notification.objects.filter(kind='reminder').exists())

    def test_invalid_pair_or_conflicting_duty_does_not_remind(self):
        self.allocation()
        self.mine.residents.set([self.a, self.c]); self.theirs.residents.set([self.a, self.c])
        refresh_request_notifications(now=self.now + timedelta(days=2))
        self.assertFalse(Notification.objects.filter(kind='reminder').exists())
        self.assertIsNotNone(Notification.objects.get().read_at)
        self.mine.residents.set([self.a, self.b]); self.theirs.residents.set([self.a, self.b])
        Shift.objects.create(external_id='notice-conflict', resident=self.b, starts_at=self.theirs.starts_at, ends_at=self.theirs.ends_at, sector='Other')
        refresh_request_notifications(now=self.now + timedelta(days=3))
        self.assertFalse(Notification.objects.filter(kind='reminder').exists())

    def test_exchange_with_new_scheduling_conflict_does_not_remind(self):
        self.exchange()
        Shift.objects.create(external_id='notice-exchange-conflict', resident=self.a, starts_at=self.requested.starts_at, ends_at=self.requested.ends_at, sector='Other')
        refresh_request_notifications(now=self.now + timedelta(days=2))
        self.assertFalse(Notification.objects.filter(kind='reminder').exists())
        self.assertIsNotNone(Notification.objects.get().read_at)

    def test_existing_read_time_is_preserved_when_request_becomes_obsolete(self):
        self.exchange()
        notice = Notification.objects.get()
        notice.read_at = self.now; notice.save(update_fields=['read_at'])
        refresh_request_notifications(now=self.now + timedelta(days=12))
        notice.refresh_from_db()
        self.assertEqual(notice.read_at, self.now)
        self.assertEqual(notice.payload['status'], 'closed')
        self.assertEqual(refresh_request_notifications(now=self.now + timedelta(days=13)), {'requests':0, 'reminders':0, 'closed':0})

    def test_proposal_rollback_also_rolls_back_notice(self):
        with transaction.atomic():
            self.exchange()
            transaction.set_rollback(True)
        self.assertFalse(Exchange.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_sync_refreshes_notices_without_google_sources(self):
        Exchange.objects.create(offered=self.offered, requested=self.requested, proposer=self.a, recipient=self.b)
        with patch('schedule.management.commands.sync_sheets.refresh_request_notifications', wraps=refresh_request_notifications) as refresh:
            call_command('sync_sheets', registered=True)
        self.assertEqual(refresh.call_count, 2)  # Before and after source updates.
        self.assertEqual(Notification.objects.filter(kind='request', resident=self.b).count(), 1)
