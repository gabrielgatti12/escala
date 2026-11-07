from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from .models import Allocation, SharedActivity, Shift
from .services import propose_allocation, respond_allocation


class AllocationAvailabilityTests(TestCase):
    def setUp(self):
        self.a = get_user_model().objects.create_user('availability-a')
        self.b = get_user_model().objects.create_user('availability-b')
        self.start = timezone.localtime(timezone.now()+timedelta(days=3)).replace(hour=8, minute=0)
        self.activities = []
        for index in range(4):
            item = SharedActivity.objects.create(external_id=f'availability-{index}', source='test', sector=f'Setor {index}', starts_at=self.start, ends_at=self.start+timedelta(hours=4))
            item.residents.set([self.a, self.b])
            self.activities.append(item)

    def test_proposal_rejects_current_conflict_for_either_resident(self):
        for resident in [self.a, self.b]:
            with self.subTest(resident=resident.pk):
                shift = Shift.objects.create(external_id=f'busy-{resident.pk}', resident=resident, sector='Já definido', starts_at=self.start, ends_at=self.start+timedelta(hours=1))
                with self.assertRaisesMessage(ValidationError, 'conflito'):
                    propose_allocation(self.a, self.activities[0].pk, self.activities[1].pk)
                shift.delete()
        self.assertFalse(Allocation.objects.exists())

    def test_acceptance_gives_exactly_one_each_and_blocks_extra_overlapping_division(self):
        proposal = propose_allocation(self.a, self.activities[0].pk, self.activities[1].pk)
        respond_allocation(self.b, proposal.pk, 'accept')
        self.assertEqual(Shift.objects.filter(resident=self.a).count(), 1)
        self.assertEqual(Shift.objects.filter(resident=self.b).count(), 1)
        with self.assertRaisesMessage(ValidationError, 'conflito'):
            propose_allocation(self.a, self.activities[2].pk, self.activities[3].pk)
        self.assertEqual(Allocation.objects.count(), 1)

    def test_cannot_submit_the_same_activity_twice_or_mix_days(self):
        with self.assertRaises(ValidationError):
            propose_allocation(self.a, self.activities[0].pk, self.activities[0].pk)
        other = self.activities[1]
        other.starts_at += timedelta(days=1);other.ends_at += timedelta(days=1);other.save()
        with self.assertRaises(ValidationError):
            propose_allocation(self.a, self.activities[0].pk, other.pk)
        self.assertFalse(Allocation.objects.exists())
