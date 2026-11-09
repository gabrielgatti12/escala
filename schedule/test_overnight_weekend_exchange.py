from datetime import timedelta
from itertools import permutations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import Exchange, Shift
from .services import exchange_group, propose_exchange, respond_exchange


class OvernightWeekendExchangeTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.a = users.objects.create_user('on-call-ana')
        self.b = users.objects.create_user('on-call-bia')
        self.c = users.objects.create_user('on-call-carla')
        self.start = timezone.now() + timedelta(days=7)
        self.number = 0

    def pair(self, offered_category, requested_category=None, groups=('R1','R1')):
        self.number += 1
        start = self.start + timedelta(days=self.number*21)
        duration = timedelta(hours=12)
        offered = Shift.objects.create(
            external_id=f'on-call-offer-{self.number}', category=offered_category,
            resident_group=groups[0], resident=self.a,
            starts_at=start, ends_at=start+duration, sector='Plantão oferecido',
        )
        requested = Shift.objects.create(
            external_id=f'on-call-request-{self.number}', category=requested_category or offered_category,
            resident_group=groups[1], resident=self.b,
            starts_at=start+timedelta(days=7), ends_at=start+timedelta(days=7)+duration,
            sector='Plantão solicitado',
        )
        return offered, requested

    def assert_owners(self, offered, requested, owners=None):
        offered.refresh_from_db(); requested.refresh_from_db()
        self.assertEqual((offered.resident_id,requested.resident_id),owners or (self.a.pk,self.b.pk))

    def test_night_and_weekend_require_recipient_acceptance(self):
        for category in ['night','weekend']:
            with self.subTest(category=category):
                offered,requested = self.pair(category)
                proposal = propose_exchange(self.a,offered.pk,requested.pk)
                self.assert_owners(offered,requested)
                for user in [self.a,self.c]:
                    with self.assertRaisesMessage(ValidationError,'Você não pode realizar esta ação'):
                        respond_exchange(user,proposal.pk,'accept')
                respond_exchange(self.b,proposal.pk,'accept')
                self.assert_owners(offered,requested,(self.b.pk,self.a.pk))
                self.assertEqual((offered.category,requested.category),(category,category))

    def test_every_cross_category_exchange_is_rejected(self):
        for offered_category,requested_category in permutations(['regular','cinderela','night','weekend'],2):
            with self.subTest(categories=(offered_category,requested_category)):
                offered,requested = self.pair(offered_category,requested_category)
                with self.assertRaisesMessage(ValidationError,'plantões do mesmo tipo'):
                    propose_exchange(self.a,offered.pk,requested.pk)
                self.assert_owners(offered,requested)
        self.assertFalse(Exchange.objects.exists())

    def test_r1_cannot_exchange_nights_or_weekends_with_r2_or_r3(self):
        for category in ['night','weekend']:
            for groups in [('R1','R2'),('R1','R3'),('R2','R1'),('R3','R1')]:
                with self.subTest(category=category,groups=groups):
                    offered,requested = self.pair(category,groups=groups)
                    with self.assertRaisesMessage(ValidationError,'Plantões de R1 só podem ser trocados entre R1'):
                        propose_exchange(self.a,offered.pk,requested.pk)
                    self.assert_owners(offered,requested)
        self.assertFalse(Exchange.objects.exists())

    def test_r2_and_r3_can_exchange_nights_and_weekends_within_their_bucket(self):
        for category in ['night','weekend']:
            for groups in [('R2','R2'),('R3','R3'),('R2','R3'),('R3','R2')]:
                with self.subTest(category=category,groups=groups):
                    offered,requested = self.pair(category,groups=groups)
                    proposal = propose_exchange(self.a,offered.pk,requested.pk)
                    self.assert_owners(offered,requested)
                    respond_exchange(self.b,proposal.pk,'accept')
                    self.assert_owners(offered,requested,(self.b.pk,self.a.pk))
                    self.assertEqual(exchange_group(offered),'R2/R3')
                    self.assertEqual(exchange_group(requested),'R2/R3')

    def test_nights_and_weekends_with_missing_or_invalid_year_are_blocked(self):
        for category in ['night','weekend']:
            for groups in [('', 'R1'),('R1',''),('R4','R1'),('R1','C1a')]:
                with self.subTest(category=category,groups=groups):
                    offered,requested = self.pair(category,groups=groups)
                    with self.assertRaisesMessage(ValidationError,'Ano do residente não definido'):
                        propose_exchange(self.a,offered.pk,requested.pk)
                    self.assert_owners(offered,requested)
        self.assertFalse(Exchange.objects.exists())

    def test_year_is_revalidated_when_accepting_nights_and_weekends(self):
        for category in ['night','weekend']:
            with self.subTest(category=category):
                offered,requested = self.pair(category)
                proposal = propose_exchange(self.a,offered.pk,requested.pk)
                Shift.objects.filter(pk=requested.pk).update(resident_group='R2')
                with self.assertRaisesMessage(ValidationError,'Plantões de R1 só podem ser trocados entre R1'):
                    respond_exchange(self.b,proposal.pk,'accept')
                self.assert_owners(offered,requested)

    def test_category_is_revalidated_when_accepting_nights_and_weekends(self):
        for category,changed in [('night','weekend'),('weekend','night')]:
            with self.subTest(category=category):
                offered,requested = self.pair(category)
                proposal = propose_exchange(self.a,offered.pk,requested.pk)
                Shift.objects.filter(pk=requested.pk).update(category=changed)
                with self.assertRaisesMessage(ValidationError,'plantões do mesmo tipo'):
                    respond_exchange(self.b,proposal.pk,'accept')
                self.assert_owners(offered,requested)

    def test_reconciliation_blocks_nights_and_weekends_at_acceptance(self):
        for category in ['night','weekend']:
            with self.subTest(category=category):
                offered,requested = self.pair(category)
                proposal = propose_exchange(self.a,offered.pk,requested.pk)
                Shift.objects.filter(pk=offered.pk).update(requires_reconciliation=True)
                with self.assertRaisesMessage(ValidationError,'precisa de conciliação'):
                    respond_exchange(self.b,proposal.pk,'accept')
                self.assert_owners(offered,requested)

    def test_regular_duty_conflict_blocks_incoming_night_or_weekend(self):
        for category in ['night','weekend']:
            with self.subTest(category=category):
                offered,requested = self.pair(category)
                Shift.objects.create(
                    external_id=f'on-call-conflict-{category}', resident=self.a,
                    starts_at=requested.starts_at, ends_at=requested.starts_at+timedelta(hours=1),
                    sector='Atividade regular',
                )
                with self.assertRaisesMessage(ValidationError,'conflito de horários'):
                    propose_exchange(self.a,offered.pk,requested.pk)
                self.assert_owners(offered,requested)
