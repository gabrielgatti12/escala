from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import Allocation, SharedActivity, Shift
from .services import propose_allocation, respond_allocation


class MirrorWeeklyAgreementTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.a = users.objects.create_user('mirror-agreement-a', first_name='Pessoa A')
        self.b = users.objects.create_user('mirror-agreement-b', first_name='Pessoa B')
        self.third = users.objects.create_user('mirror-agreement-third')
        day = timezone.localtime(timezone.now()+timedelta(days=8))
        self.monday = (day-timedelta(days=day.weekday())).replace(hour=8,minute=0,second=0,microsecond=0)
        self.pairs = []
        for offset in range(5):
            start = self.monday+timedelta(days=offset)
            pair = []
            for sector in ['INSTITUTO · Ambulatório', 'HOSPITAL · Centro Cirúrgico']:
                item = SharedActivity.objects.create(external_id=f'mirror-{offset}-{len(pair)}',
                    source='mirror-consent-fixture', sector=sector, starts_at=start,
                    ends_at=start+timedelta(hours=4))
                item.residents.set([self.a, self.b])
                pair.append(item)
            self.pairs.append(pair)

    def test_same_person_can_take_same_sector_all_five_days_after_each_acceptance(self):
        for mine, theirs in self.pairs:
            proposal = propose_allocation(self.a,mine.pk,theirs.pk)
            respond_allocation(self.b,proposal.pk,'accept')
        self.assertEqual(Shift.objects.filter(resident=self.a,sector='INSTITUTO · Ambulatório').count(),5)
        self.assertEqual(Shift.objects.filter(resident=self.b,sector='HOSPITAL · Centro Cirúrgico').count(),5)
        self.assertEqual(Allocation.objects.filter(status='accepted').count(),5)

    def test_either_person_can_propose_uneven_week_without_compensating_quota(self):
        for offset, (first, second) in enumerate(self.pairs):
            # A takes Ambulatorio four days, B one, with proposals by either side.
            proposer,recipient = (self.a,self.b) if offset%2==0 else (self.b,self.a)
            a_gets_first = offset != 4
            mine,theirs = (first,second) if (proposer==self.a)==a_gets_first else (second,first)
            proposal=propose_allocation(proposer,mine.pk,theirs.pk)
            respond_allocation(recipient,proposal.pk,'accept')
        self.assertEqual(Shift.objects.filter(resident=self.a,sector='INSTITUTO · Ambulatório').count(),4)
        self.assertEqual(Shift.objects.filter(resident=self.b,sector='INSTITUTO · Ambulatório').count(),1)

    def test_pending_proposal_never_enters_either_final_calendar_or_feed(self):
        first,second=self.pairs[0]
        proposal=propose_allocation(self.a,first.pk,second.pk)
        self.assertFalse(Shift.objects.exists())
        self.assertFalse(SharedActivity.objects.filter(allocated_to__isnull=False).exists())
        for resident in [self.a,self.b]:
            self.client.force_login(resident)
            response=self.client.get('/calendario.ics')
            self.assertNotContains(response,'BEGIN:VEVENT')
        with self.assertRaises(ValidationError):respond_allocation(self.a,proposal.pk,'accept')
        with self.assertRaises(ValidationError):respond_allocation(self.third,proposal.pk,'accept')
        self.assertFalse(Shift.objects.exists())
        respond_allocation(self.b,proposal.pk,'accept')
        self.assertEqual(Shift.objects.count(),2)

    def test_rejection_and_cancellation_keep_both_activities_unassigned(self):
        for index, action in enumerate(['reject','cancel']):
            mine,theirs=self.pairs[index]
            proposal=propose_allocation(self.a,mine.pk,theirs.pk)
            respond_allocation(self.b if action=='reject' else self.a,proposal.pk,action)
        self.assertFalse(Shift.objects.exists())
        self.assertFalse(SharedActivity.objects.filter(allocated_to__isnull=False).exists())

    def test_review_shows_consent_of_both_residents(self):
        first,second=self.pairs[0]
        proposal=propose_allocation(self.a,first.pk,second.pk)
        self.client.force_login(self.b)
        response=self.client.get('/?tab=requests')
        self.assertContains(response,'Pessoa A já concordou ao enviar')
        self.assertContains(response,'Aguardando o aceite de Pessoa B')
        self.assertContains(response,'Concordo com a divisão e atualizo a escala')
        respond_allocation(self.b,proposal.pk,'accept')
        response=self.client.get('/?tab=requests&box=history')
        self.assertContains(response,'Divisão aceita pelos dois residentes')

    def test_negotiation_choices_exclude_regular_and_single_resident_activities(self):
        start=self.monday
        Shift.objects.create(external_id='ordinary-confirmed',resident=self.a,starts_at=start+timedelta(hours=6),
            ends_at=start+timedelta(hours=8),sector='Atividade comum já definida')
        single=SharedActivity.objects.create(external_id='only-one-name',source='mirror-consent-fixture',
            starts_at=start,ends_at=start+timedelta(hours=4),sector='Atividade com apenas um residente')
        single.residents.add(self.a)
        self.client.force_login(self.a)
        response=self.client.get('/',{'week':timezone.localdate(self.monday).isoformat()})
        choices=[item['activity'] for item in response.context['allocation_options']]
        self.assertNotIn(single.pk,{item.pk for item in choices})
        self.assertNotIn('Atividade comum já definida',{item.sector for item in choices})
        with self.assertRaises(ValidationError):
            propose_allocation(self.a,single.pk,self.pairs[0][1].pk)
        self.assertFalse(Allocation.objects.exists())

    def test_one_days_acceptance_does_not_confirm_another_days_proposal(self):
        accepted=propose_allocation(self.a,self.pairs[0][0].pk,self.pairs[0][1].pk)
        pending=propose_allocation(self.a,self.pairs[1][0].pk,self.pairs[1][1].pk)
        respond_allocation(self.b,accepted.pk,'accept')
        pending.refresh_from_db()
        self.assertEqual(pending.status,'pending')
        self.assertEqual(Shift.objects.count(),2)
        for activity in self.pairs[1]:
            activity.refresh_from_db()
            self.assertIsNone(activity.allocated_to_id)
        self.client.force_login(self.a)
        self.assertContains(self.client.get('/calendario.ics'),'BEGIN:VEVENT',count=1)
