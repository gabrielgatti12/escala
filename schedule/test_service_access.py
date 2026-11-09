from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from .models import Allocation, Exchange, ForumComment, ResidentAccess, SharedActivity, Shift
from .services import (
    add_forum_comment, propose_allocation, propose_exchange, respond_allocation,
    respond_exchange, set_exchange_interest, withdraw_forum_topic,
)


class DisabledResidentServiceTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.a = users.objects.create_user('service-access-ana')
        self.b = users.objects.create_user('service-access-bia')
        self.alias = users.objects.create_user('service-access@example.test')
        ResidentAccess.objects.create(account=self.alias, resident=self.a)
        start = timezone.now() + timedelta(days=7)
        self.offered = Shift.objects.create(external_id='service-access-offer', resident=self.a, starts_at=start, ends_at=start + timedelta(hours=4), sector='Hospital')
        self.requested = Shift.objects.create(external_id='service-access-request', resident=self.b, starts_at=start + timedelta(days=1), ends_at=start + timedelta(days=1, hours=4), sector='Instituto')
        self.mine = SharedActivity.objects.create(external_id='service-access-mine', source='access-test', starts_at=start + timedelta(days=2), ends_at=start + timedelta(days=2, hours=4), sector='Ambulatório')
        self.theirs = SharedActivity.objects.create(external_id='service-access-theirs', source='access-test', starts_at=start + timedelta(days=2), ends_at=start + timedelta(days=2, hours=4), sector='Wet lab')
        self.mine.residents.set([self.a,self.b]); self.theirs.residents.set([self.a,self.b])

    def set_active(self, resident, active):
        # Deliberately keep the caller's User instance cached: authorization
        # must honor the persisted revocation, not an old is_active attribute.
        get_user_model().objects.filter(pk=resident.pk).update(is_active=active)

    def assert_original_owners(self):
        self.offered.refresh_from_db(); self.requested.refresh_from_db()
        self.assertEqual((self.offered.resident_id,self.requested.resident_id),(self.a.pk,self.b.pk))

    def test_alias_cannot_propose_for_disabled_source_identity_even_with_stale_user(self):
        self.set_active(self.a,False)
        self.assertTrue(self.alias.is_active)
        self.assertTrue(self.a.is_active)
        with self.assertRaisesMessage(ValidationError,'está desativado'):
            propose_exchange(self.a,self.offered.pk,self.requested.pk)
        self.assertFalse(Exchange.objects.exists())
        self.assert_original_owners()

    def test_cannot_propose_exchange_to_disabled_recipient(self):
        self.set_active(self.b,False)
        with self.assertRaisesMessage(ValidationError,'está desativado'):
            propose_exchange(self.a,self.offered.pk,self.requested.pk)
        self.assertFalse(Exchange.objects.exists())

    def test_exchange_responses_require_both_participants_to_remain_active(self):
        proposal = propose_exchange(self.a,self.offered.pk,self.requested.pk)
        for resident in [self.a,self.b]:
            for user,action in [(self.a,'cancel'),(self.b,'accept'),(self.b,'reject')]:
                with self.subTest(disabled=resident.pk,action=action):
                    self.set_active(resident,False)
                    with self.assertRaisesMessage(ValidationError,'está desativado'):
                        respond_exchange(user,proposal.pk,action)
                    proposal.refresh_from_db()
                    self.assertEqual(proposal.status,'pending')
                    self.assert_original_owners()
                    self.set_active(resident,True)

    def test_allocation_requires_active_actor_and_every_member_of_both_activities(self):
        for resident in [self.a,self.b]:
            with self.subTest(disabled=resident.pk):
                self.set_active(resident,False)
                with self.assertRaisesMessage(ValidationError,'está desativado'):
                    propose_allocation(self.a,self.mine.pk,self.theirs.pk)
                self.assertFalse(Allocation.objects.exists())
                self.set_active(resident,True)

    def test_allocation_responses_require_active_proposer_and_recipient(self):
        proposal = propose_allocation(self.a,self.mine.pk,self.theirs.pk)
        for resident in [self.a,self.b]:
            for user,action in [(self.a,'cancel'),(self.b,'accept'),(self.b,'reject')]:
                with self.subTest(disabled=resident.pk,action=action):
                    self.set_active(resident,False)
                    with self.assertRaisesMessage(ValidationError,'está desativado'):
                        respond_allocation(user,proposal.pk,action)
                    proposal.refresh_from_db()
                    self.assertEqual(proposal.status,'pending')
                    self.assertFalse(SharedActivity.objects.filter(allocated_to__isnull=False).exists())
                    self.set_active(resident,True)

    def test_disabled_actor_cannot_publish_interest(self):
        self.set_active(self.a,False)
        with self.assertRaisesMessage(ValidationError,'está desativado'):
            set_exchange_interest(self.a,self.offered.pk)
        self.offered.refresh_from_db()
        self.assertFalse(self.offered.wants_exchange)
        self.assertFalse(self.offered.exchange_topics.exists())

    def test_disabled_actor_cannot_comment_on_open_topic(self):
        topic = set_exchange_interest(self.b,self.requested.pk)
        self.set_active(self.a,False)
        with self.assertRaisesMessage(ValidationError,'está desativado'):
            add_forum_comment(self.a,topic.pk,'Este comentário deve ser impedido.')
        self.assertFalse(ForumComment.objects.exists())

    def test_disabled_poster_cannot_withdraw_topic(self):
        topic = set_exchange_interest(self.a,self.offered.pk)
        self.set_active(self.a,False)
        with self.assertRaisesMessage(ValidationError,'está desativado'):
            withdraw_forum_topic(self.a,topic.pk)
        topic.refresh_from_db(); self.offered.refresh_from_db()
        self.assertIsNone(topic.closed_at)
        self.assertTrue(self.offered.wants_exchange)

    def test_sqlite_busy_response_is_a_retryable_conflict_without_changing_request(self):
        proposal = propose_exchange(self.a,self.offered.pk,self.requested.pk)
        self.client.force_login(self.b)
        for error in ['database is locked','database table is locked: schedule_exchange']:
            with self.subTest(error=error):
                with patch('schedule.views.respond_exchange',side_effect=OperationalError(error)):
                    response = self.client.post(f'/trocas/{proposal.pk}/accept/')
                self.assertEqual(response.status_code,409)
                self.assertGreater(int(response['Retry-After']),0)
                self.assertEqual(response.templates,[])
                self.assertIn('no-store',response['Cache-Control'])
                proposal.refresh_from_db()
                self.assertEqual(proposal.status,'pending')
                self.assert_original_owners()

    def test_other_database_errors_remain_server_errors(self):
        proposal = propose_exchange(self.a,self.offered.pk,self.requested.pk)
        self.client.force_login(self.b)
        self.client.raise_request_exception = False
        with patch('schedule.views.respond_exchange',side_effect=OperationalError('no such table: fixture_missing')):
            with self.assertLogs('django.request',level='ERROR'):
                response = self.client.post(f'/trocas/{proposal.pk}/accept/')
        self.assertEqual(response.status_code,500)
        self.assertNotIn('Retry-After',response)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status,'pending')
        self.assert_original_owners()
