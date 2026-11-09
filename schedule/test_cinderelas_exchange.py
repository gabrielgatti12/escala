from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import Exchange, Shift
from .services import exchange_group, propose_exchange, respond_exchange


class CinderelaExchangeTests(TestCase):
    def setUp(self):
        self.a = get_user_model().objects.create_user('cinderela-ana')
        self.b = get_user_model().objects.create_user('cinderela-bia')
        self.c = get_user_model().objects.create_user('cinderela-carla')
        self.start = timezone.localtime(timezone.now() + timedelta(days=7)).replace(hour=18, minute=0, second=0, microsecond=0)
        self.offered = Shift.objects.create(
            external_id='cinderela-offered', category='cinderela', resident_group='R1', resident=self.a,
            starts_at=self.start, ends_at=self.start + timedelta(hours=6),
            sector='Cinderela',
        )
        self.requested = Shift.objects.create(
            external_id='cinderela-requested', category='cinderela', resident_group='R1', resident=self.b,
            starts_at=self.start + timedelta(days=1), ends_at=self.start + timedelta(days=1, hours=6),
            sector='Cinderela',
        )

    def proposal(self):
        return propose_exchange(self.a, self.offered.pk, self.requested.pk)

    def assert_original_owners(self):
        self.offered.refresh_from_db(); self.requested.refresh_from_db()
        self.assertEqual((self.offered.resident_id, self.requested.resident_id), (self.a.pk, self.b.pk))

    def test_cinderela_exchange_updates_owners_only_after_recipient_accepts(self):
        proposal = self.proposal()
        self.assert_original_owners()
        for user in [self.a, self.c]:
            with self.subTest(user=user.pk):
                with self.assertRaisesMessage(ValidationError, 'Você não pode realizar esta ação'):
                    respond_exchange(user, proposal.pk, 'accept')
        respond_exchange(self.b, proposal.pk, 'accept')
        self.offered.refresh_from_db(); self.requested.refresh_from_db()
        self.assertEqual((self.offered.resident_id, self.requested.resident_id), (self.b.pk, self.a.pk))
        self.assertEqual((self.offered.category, self.requested.category), ('cinderela', 'cinderela'))

    def test_cinderela_and_regular_activity_cannot_be_exchanged_in_either_direction(self):
        for category_a, category_b in [('cinderela', 'regular'), ('regular', 'cinderela')]:
            with self.subTest(categories=(category_a, category_b)):
                Shift.objects.filter(pk=self.offered.pk).update(category=category_a)
                Shift.objects.filter(pk=self.requested.pk).update(category=category_b)
                with self.assertRaisesMessage(ValidationError, 'plantões do mesmo tipo'):
                    self.proposal()
                self.assertFalse(Exchange.objects.exists())
                self.assert_original_owners()

    def test_r1_cannot_exchange_with_r2_or_r3_in_either_direction(self):
        for group_a, group_b in [('R1', 'R2'), ('R1', 'R3'), ('R2', 'R1'), ('R3', 'R1')]:
            with self.subTest(groups=(group_a, group_b)):
                Shift.objects.filter(pk=self.offered.pk).update(resident_group=group_a)
                Shift.objects.filter(pk=self.requested.pk).update(resident_group=group_b)
                with self.assertRaisesMessage(ValidationError, 'Plantões de R1 só podem ser trocados entre R1'):
                    self.proposal()
                self.assertFalse(Exchange.objects.exists())
                self.assert_original_owners()

    def test_r2_and_r3_can_exchange_cinderelas_after_mutual_confirmation(self):
        Shift.objects.filter(pk=self.offered.pk).update(resident_group='R2')
        Shift.objects.filter(pk=self.requested.pk).update(resident_group='R3')
        proposal = self.proposal()
        self.assert_original_owners()
        respond_exchange(self.b, proposal.pk, 'accept')
        self.offered.refresh_from_db(); self.requested.refresh_from_db()
        self.assertEqual((self.offered.resident_id, self.requested.resident_id), (self.b.pk, self.a.pk))
        self.assertEqual(exchange_group(self.offered), exchange_group(self.requested))

    def test_cinderela_with_unknown_year_blocks_both_sides(self):
        for shift in [self.offered, self.requested]:
            for group in ['', 'R4', 'C1a']:
                with self.subTest(shift=shift.pk, group=group):
                    Shift.objects.filter(pk=shift.pk).update(resident_group=group)
                    with self.assertRaisesMessage(ValidationError, 'Ano do residente não definido'):
                        self.proposal()
                    self.assertFalse(Exchange.objects.exists())
                    self.assert_original_owners()
                    Shift.objects.filter(pk=shift.pk).update(resident_group='R1')

    def test_changed_year_is_checked_again_at_acceptance(self):
        proposal = self.proposal()
        Shift.objects.filter(pk=self.requested.pk).update(resident_group='R2')
        with self.assertRaisesMessage(ValidationError, 'Plantões de R1 só podem ser trocados entre R1'):
            respond_exchange(self.b, proposal.pk, 'accept')
        self.assert_original_owners()

    def test_regular_activities_do_not_need_cinderela_year(self):
        Shift.objects.filter(pk__in=[self.offered.pk,self.requested.pk]).update(category='regular',resident_group='')
        proposal = self.proposal()
        respond_exchange(self.b, proposal.pk, 'accept')
        self.requested.refresh_from_db()
        self.assertEqual(self.requested.resident_id, self.a.pk)

    def test_reconciliation_blocks_either_side_before_proposing(self):
        for shift in [self.offered, self.requested]:
            with self.subTest(shift=shift.pk):
                Shift.objects.filter(pk=shift.pk).update(requires_reconciliation=True)
                with self.assertRaisesMessage(ValidationError, 'Confira a planilha de origem'):
                    self.proposal()
                self.assertFalse(Exchange.objects.exists())
                self.assert_original_owners()
                Shift.objects.filter(pk=shift.pk).update(requires_reconciliation=False)

    def test_reconciliation_blocks_existing_proposal_at_acceptance(self):
        proposal = self.proposal()
        Shift.objects.filter(pk=self.requested.pk).update(requires_reconciliation=True)
        with self.assertRaisesMessage(ValidationError, 'precisa de conciliação'):
            respond_exchange(self.b, proposal.pk, 'accept')
        self.assert_original_owners()
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')

    def test_changed_category_is_revalidated_at_acceptance(self):
        proposal = self.proposal()
        Shift.objects.filter(pk=self.requested.pk).update(category='regular')
        with self.assertRaisesMessage(ValidationError, 'plantões do mesmo tipo'):
            respond_exchange(self.b, proposal.pk, 'accept')
        self.assert_original_owners()

    def test_regular_duty_overlapping_incoming_cinderela_blocks_proposal(self):
        Shift.objects.create(
            external_id='regular-overlap', resident=self.a, category='regular',
            starts_at=self.requested.starts_at - timedelta(hours=1),
            ends_at=self.requested.starts_at + timedelta(hours=1), sector='Ambulatório',
        )
        with self.assertRaisesMessage(ValidationError, 'conflito de horários'):
            self.proposal()
        self.assert_original_owners()

    def test_regular_duty_ending_at_18_does_not_overlap_cinderela(self):
        Shift.objects.create(
            external_id='regular-adjacent', resident=self.a, category='regular',
            starts_at=self.requested.starts_at - timedelta(hours=10),
            ends_at=self.requested.starts_at, sector='Ambulatório',
        )
        proposal = self.proposal()
        respond_exchange(self.b, proposal.pk, 'accept')
        self.requested.refresh_from_db()
        self.assertEqual(self.requested.resident_id, self.a.pk)

    def test_new_conflict_is_checked_again_when_accepting_cinderela(self):
        proposal = self.proposal()
        Shift.objects.create(
            external_id='regular-new-conflict', resident=self.b, category='regular',
            starts_at=self.offered.starts_at + timedelta(hours=1),
            ends_at=self.offered.ends_at, sector='Emergência',
        )
        with self.assertRaisesMessage(ValidationError, 'conflito de horários'):
            respond_exchange(self.b, proposal.pk, 'accept')
        self.assert_original_owners()
