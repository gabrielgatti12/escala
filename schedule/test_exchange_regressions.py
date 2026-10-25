from datetime import datetime, timedelta, timezone as utc

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import Allocation, Exchange, SharedActivity, Shift
from .services import expire_pending_requests, propose_allocation, propose_exchange, respond_allocation


class AllocationValidationTests(TestCase):
    def setUp(self):
        self.proposer = get_user_model().objects.create_user('division-proposer')
        self.recipient = get_user_model().objects.create_user('division-recipient')
        self.other = get_user_model().objects.create_user('division-other')
        self.start = timezone.now() + timedelta(days=5)

    def activities(self, first_start=None, second_start=None):
        first_start = first_start or self.start
        second_start = second_start or first_start
        items = []
        for name, start in [('first', first_start), ('second', second_start)]:
            activity = SharedActivity.objects.create(
                external_id=f'division-{name}', source='validation-test',
                starts_at=start, ends_at=start + timedelta(hours=6), sector=name,
            )
            activity.residents.set([self.proposer, self.recipient])
            items.append(activity)
        return items

    def test_cannot_propose_division_for_past_activities(self):
        mine, theirs = self.activities(timezone.now() - timedelta(days=1))
        with self.assertRaisesMessage(ValidationError, 'atividades futuras'):
            propose_allocation(self.proposer, mine.pk, theirs.pk)
        self.assertFalse(Allocation.objects.exists())

    def test_cannot_accept_division_after_activity_starts(self):
        mine, theirs = self.activities()
        proposal = propose_allocation(self.proposer, mine.pk, theirs.pk)
        SharedActivity.objects.update(
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        with self.assertRaisesMessage(ValidationError, 'atividades futuras'):
            respond_allocation(self.recipient, proposal.pk, 'accept')
        self.assertFalse(Shift.objects.exists())
        self.assertFalse(SharedActivity.objects.filter(allocated_to__isnull=False).exists())

    def test_old_recipient_cannot_accept_after_pair_changes(self):
        mine, theirs = self.activities()
        proposal = propose_allocation(self.proposer, mine.pk, theirs.pk)
        mine.residents.set([self.proposer, self.other])
        theirs.residents.set([self.proposer, self.other])
        with self.assertRaisesMessage(ValidationError, 'dupla destas atividades mudou'):
            respond_allocation(self.recipient, proposal.pk, 'accept')
        self.assertFalse(Shift.objects.exists())
        self.assertFalse(SharedActivity.objects.filter(allocated_to__isnull=False).exists())

    def test_same_local_day_can_cross_utc_midnight(self):
        mine, theirs = self.activities(
            datetime(2040, 5, 20, 23, tzinfo=utc.utc),
            datetime(2040, 5, 21, 1, tzinfo=utc.utc),
        )
        with timezone.override('America/Sao_Paulo'):
            proposal = propose_allocation(self.proposer, mine.pk, theirs.pk)
            respond_allocation(self.recipient, proposal.pk, 'accept')
        self.assertEqual(Shift.objects.count(), 2)

    def test_distinct_local_days_cannot_be_divided_even_on_same_utc_day(self):
        mine, theirs = self.activities(
            datetime(2040, 5, 21, 2, tzinfo=utc.utc),
            datetime(2040, 5, 21, 4, tzinfo=utc.utc),
        )
        with timezone.override('America/Sao_Paulo'):
            with self.assertRaisesMessage(ValidationError, 'mesmo dia'):
                propose_allocation(self.proposer, mine.pk, theirs.pk)
        self.assertFalse(Allocation.objects.exists())


class PendingRequestExpirationTests(TestCase):
    def setUp(self):
        self.a = get_user_model().objects.create_user('expiry-a')
        self.b = get_user_model().objects.create_user('expiry-b')
        self.now = timezone.now().replace(microsecond=0)
        self.starts = self.now + timedelta(days=2)
        self.offered = Shift.objects.create(
            external_id='expiry-offer', resident=self.a,
            starts_at=self.starts, ends_at=self.starts + timedelta(hours=4), sector='A',
        )
        self.requested = Shift.objects.create(
            external_id='expiry-request', resident=self.b,
            starts_at=self.starts + timedelta(days=1),
            ends_at=self.starts + timedelta(days=1, hours=4), sector='B',
        )
        self.exchange = propose_exchange(self.a, self.offered.pk, self.requested.pk)
        activities = []
        for name in ['first', 'second']:
            activity = SharedActivity.objects.create(
                external_id=f'expiry-{name}', source='expiry',
                starts_at=self.starts + timedelta(days=3),
                ends_at=self.starts + timedelta(days=3, hours=4), sector=name,
            )
            activity.residents.set([self.a, self.b])
            activities.append(activity)
        self.mine, self.theirs = activities
        self.allocation = propose_allocation(self.a, self.mine.pk, self.theirs.pk)

    def test_expiration_cancels_only_pending_requests_with_a_started_activity(self):
        Shift.objects.filter(pk=self.requested.pk).update(
            starts_at=self.now, ends_at=self.now + timedelta(hours=4),
        )
        SharedActivity.objects.filter(pk=self.theirs.pk).update(
            starts_at=self.now - timedelta(hours=1), ends_at=self.now + timedelta(hours=3),
        )
        accepted = Exchange.objects.create(
            offered=self.offered, requested=self.requested, proposer=self.a,
            recipient=self.b, status='accepted', resolved_at=self.now - timedelta(days=1),
        )
        rejected = Allocation.objects.create(
            mine=self.mine, theirs=self.theirs, proposer=self.a,
            recipient=self.b, status='rejected', resolved_at=self.now - timedelta(days=1),
        )
        self.assertEqual(expire_pending_requests(self.now), {'exchanges':1, 'allocations':1})
        for proposal in [self.exchange, self.allocation]:
            proposal.refresh_from_db()
            self.assertEqual(proposal.status, 'cancelled')
            self.assertEqual(proposal.resolved_at, self.now)
        accepted.refresh_from_db(); rejected.refresh_from_db()
        self.assertEqual((accepted.status, rejected.status), ('accepted', 'rejected'))
        self.assertEqual(Shift.objects.count(), 2)
        self.assertFalse(SharedActivity.objects.filter(allocated_to__isnull=False).exists())
        self.assertEqual(expire_pending_requests(self.now), {'exchanges':0, 'allocations':0})

    def test_future_requests_do_not_expire(self):
        self.assertEqual(expire_pending_requests(self.now), {'exchanges':0, 'allocations':0})
        self.exchange.refresh_from_db(); self.allocation.refresh_from_db()
        self.assertEqual((self.exchange.status, self.allocation.status), ('pending', 'pending'))
        self.assertFalse(self.exchange.is_expired)
        self.assertFalse(self.allocation.is_expired)

    def test_expired_properties_cover_unprocessed_pending_requests(self):
        self.requested.starts_at = self.now - timedelta(hours=1)
        self.exchange.requested = self.requested
        self.theirs.starts_at = self.now - timedelta(hours=1)
        self.allocation.theirs = self.theirs
        self.assertTrue(self.exchange.is_expired)
        self.assertTrue(self.allocation.is_expired)
        self.exchange.status = self.allocation.status = 'accepted'
        self.assertFalse(self.exchange.is_expired)
        self.assertFalse(self.allocation.is_expired)

    def test_sync_cycle_expires_requests_even_without_registered_sources(self):
        from django.core.management import call_command
        Shift.objects.filter(pk=self.requested.pk).update(
            starts_at=self.now - timedelta(hours=1), ends_at=self.now + timedelta(hours=1),
        )
        call_command('sync_sheets', registered=True)
        self.exchange.refresh_from_db()
        self.assertEqual(self.exchange.status, 'cancelled')
