from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .bundle_exchanges import propose_bundle, respond_bundle, review_bundle, review_pending_bundle
from .models import BundleExchange, BundleExchangeItem, Exchange, IncompleteActivity, SharedActivity, Shift


class BundleExchangeTests(TestCase):
    def setUp(self):
        users=get_user_model()
        self.a=users.objects.create_user('bundle-owner',first_name='Pessoa A')
        self.b=users.objects.create_user('bundle-colleague',first_name='Pessoa B')
        self.third=users.objects.create_user('bundle-third')
        self.day=timezone.localtime(timezone.now()+timedelta(days=7)).replace(hour=0,minute=0,second=0,microsecond=0)
        self.pr=self.shift('pr',self.a,7,45,12,0,'INSTITUTO · Projeto Retina')
        self.cc=self.shift('cc',self.a,13,0,19,0,'HOSPITAL · CC Leonardo')
        self.pre=self.shift('pre',self.b,7,40,17,0,'INSTITUTO · Pré-Operatório')

    def shift(self,key,person,hour,minute,end_hour,end_minute,sector,**extra):
        return Shift.objects.create(external_id=f'bundle-{key}',resident=person,
            starts_at=self.day.replace(hour=hour,minute=minute),
            ends_at=self.day.replace(hour=end_hour,minute=end_minute),sector=sector,source='bundle-test-source',
            source_cell=key,**extra)

    def report(self,mode='day'):
        return review_bundle(self.a,self.b,[self.pr.pk,self.cc.pk],[self.pre.pk],mode)

    def proposal(self,mode='day'):
        report=self.report(mode)
        return propose_bundle(self.a,self.b,[self.pr.pk,self.cc.pk],[self.pre.pk],mode,
                              review_fingerprint=report['fingerprint'],acknowledged=True)

    def accept(self,proposal):
        report=review_pending_bundle(proposal)
        return respond_bundle(self.b,proposal.pk,'accept',review_fingerprint=report['fingerprint'],acknowledged=True)

    def owners(self):
        return list(Shift.objects.filter(pk__in=[self.pr.pk,self.cc.pk,self.pre.pk]).order_by('pk').values_list('resident_id',flat=True))

    def test_two_for_one_report_uses_exact_imported_workload_and_requires_acknowledgement(self):
        report=self.report()
        self.assertEqual(report['proposer']['outgoing_minutes'],615)
        self.assertEqual(report['proposer']['incoming_minutes'],560)
        self.assertEqual(report['proposer']['delta_minutes'],-55)
        self.assertEqual(report['recipient']['delta_minutes'],55)
        self.assertEqual(report['proposer']['outgoing_label'],'10h15')
        self.assertEqual(report['proposer']['incoming_label'],'9h20')
        self.assertEqual({item['code'] for item in report['warnings']},{'workload','transition'})
        for kwargs in [{},{'review_fingerprint':report['fingerprint']},{'review_fingerprint':'forged','acknowledged':True}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValidationError):
                propose_bundle(self.a,self.b,[self.pr.pk,self.cc.pk],[self.pre.pk],**kwargs)
        self.assertFalse(BundleExchange.objects.exists())

    def test_proposal_is_private_pending_and_only_recipient_can_accept_after_review(self):
        before=self.owners();proposal=self.proposal()
        self.assertEqual(self.owners(),before)
        self.assertEqual(proposal.items.count(),3)
        self.assertIsNotNone(proposal.proposer_acknowledged_at)
        self.assertIsNone(proposal.recipient_acknowledged_at)
        for actor in [self.a,self.third]:
            with self.assertRaises(ValidationError):
                respond_bundle(actor,proposal.pk,'accept',review_fingerprint=proposal.fingerprint,acknowledged=True)
        with self.assertRaises(ValidationError):respond_bundle(self.b,proposal.pk,'accept')
        accepted=self.accept(proposal)
        self.assertEqual(accepted.status,'accepted')
        self.assertIsNotNone(accepted.recipient_acknowledged_at)
        self.assertEqual(self.owners(),[self.b.pk,self.b.pk,self.a.pk])
        self.assertEqual(Shift.objects.count(),3)
        self.assertEqual(set(BundleExchangeItem.objects.filter(exchange=proposal,side='offered').values_list('original_owner_id',flat=True)),{self.a.pk})
        with self.assertRaises(ValidationError):self.accept(accepted)

    def test_decline_and_cancel_do_not_change_calendar(self):
        for actor,action in [(self.b,'reject'),(self.a,'cancel')]:
            before=self.owners();proposal=self.proposal()
            result=respond_bundle(actor,proposal.pk,action)
            self.assertEqual(result.status,'rejected' if action=='reject' else 'cancelled')
            self.assertEqual(self.owners(),before)

    def test_wrong_owner_inactive_source_past_monthly_and_reconciliation_cannot_be_acknowledged_away(self):
        changes=[{'resident':self.third},{'active':False},{'requires_reconciliation':True},
                 {'category':'night','resident_group':'R2'},
                 {'starts_at':self.day-timedelta(days=100),'ends_at':self.day-timedelta(days=99)}]
        for change in changes:
            with self.subTest(change=change):
                old={field:getattr(self.pr,field) for field in change}
                for field,value in change.items():setattr(self.pr,field,value)
                self.pr.save()
                with self.assertRaises(ValidationError):self.report()
                for field,value in old.items():setattr(self.pr,field,value)
                self.pr.save()
        self.b.is_active=False;self.b.save()
        with self.assertRaises(ValidationError):self.report()
        self.assertFalse(BundleExchange.objects.exists())

    def test_day_mode_is_complete_but_selected_can_exchange_a_subset(self):
        with self.assertRaisesMessage(ValidationError,'todos os estágios'):
            review_bundle(self.a,self.b,[self.pr.pk],[self.pre.pk],'day')
        report=review_bundle(self.a,self.b,[self.pr.pk],[self.pre.pk],'selected')
        proposal=propose_bundle(self.a,self.b,[self.pr.pk],[self.pre.pk],'selected',review_fingerprint=report['fingerprint'],acknowledged=True)
        self.accept(proposal)
        self.cc.refresh_from_db();self.assertEqual(self.cc.resident_id,self.a.pk)
        self.assertEqual(self.owners(),[self.b.pk,self.a.pk,self.a.pk])

    def test_undefined_activities_block_whole_day_and_are_disclosed_for_selected(self):
        shared=SharedActivity.objects.create(external_id='bundle-shared',source='fixture',sector='A combinar',starts_at=self.day.replace(hour=8),ends_at=self.day.replace(hour=12))
        shared.residents.set([self.a,self.third])
        with self.assertRaisesMessage(ValidationError,'a combinar ou sem horário'):self.report()
        selected=self.report('selected')
        self.assertIn('undefined',{warning['code'] for warning in selected['warnings']})
        shared.delete()
        unknown=IncompleteActivity.objects.create(external_id='bundle-unknown',source='fixture',date=self.day.date(),sector='Sem horário')
        unknown.residents.add(self.b)
        with self.assertRaisesMessage(ValidationError,'a combinar ou sem horário'):self.report()

    def test_changed_chosen_terms_or_kept_commitment_invalidates_both_approvals(self):
        for change in ['time','source','owner','kept']:
            with self.subTest(change=change):
                proposal=self.proposal('selected')
                before=self.owners()
                if change=='time':
                    self.pre.ends_at+=timedelta(minutes=10);self.pre.save()
                elif change=='source':
                    self.pre.source_cell='different-cell';self.pre.save()
                elif change=='owner':
                    self.pre.resident=self.third;self.pre.save()
                else:
                    kept=self.shift('new-kept',self.a,19,30,20,0,'Novo compromisso')
                with self.assertRaises(ValidationError):
                    respond_bundle(self.b,proposal.pk,'accept',review_fingerprint=proposal.fingerprint,acknowledged=True)
                self.pr.refresh_from_db();self.cc.refresh_from_db()
                self.assertEqual((self.pr.resident_id,self.cc.resident_id),(self.a.pk,self.a.pk))
                proposal.refresh_from_db();self.assertEqual(proposal.status,'pending')
                respond_bundle(self.a,proposal.pk,'cancel')
                self.pre.resident=self.b;self.pre.source_cell='pre';self.pre.ends_at=self.day.replace(hour=17);self.pre.save()
                if change=='kept':kept.delete()

    def test_overlap_and_neighbor_intervals_are_reviewed_by_both_and_can_be_acknowledged(self):
        kept=self.shift('kept',self.a,16,30,18,0,'HOSPITAL · Compromisso mantido')
        next_item=self.shift('next',self.b,19,30,20,0,'INSTITUTO · Setor seguinte')
        report=self.report('selected')
        warnings=report['warnings']
        self.assertIn('overlap',{item['code'] for item in warnings})
        overlap=next(item for item in warnings if item['code']=='overlap')
        self.assertIn('16:30',overlap['message']);self.assertIn('17:00',overlap['message'])
        transition=next(item for item in warnings if item['code']=='transition' and '19:00' in item['message'])
        self.assertIn('19:00',transition['message']);self.assertIn('19:30',transition['message'])
        proposal=self.proposal('selected')
        with self.assertRaises(ValidationError):respond_bundle(self.b,proposal.pk,'accept',review_fingerprint=proposal.fingerprint,acknowledged=False)
        self.accept(proposal)
        kept.refresh_from_db();next_item.refresh_from_db()
        self.assertEqual((kept.resident_id,next_item.resident_id),(self.a.pk,self.b.pk))
        self.assertEqual(self.owners(),[self.b.pk,self.b.pk,self.a.pk])

    def test_additional_day_activity_after_proposal_invalidates_full_day_acceptance(self):
        proposal=self.proposal()
        self.shift('late-added',self.a,20,0,21,0,'Nova atividade do dia')
        with self.assertRaisesMessage(ValidationError,'todos os estágios'):self.accept(proposal)
        self.assertEqual(self.owners(),[self.a.pk,self.a.pk,self.b.pk])

    def test_distinct_days_allowed_but_each_side_must_use_one_day(self):
        self.pre.starts_at+=timedelta(days=1);self.pre.ends_at+=timedelta(days=1);self.pre.save()
        proposal=self.proposal();self.accept(proposal)
        self.assertEqual(self.owners(),[self.b.pk,self.b.pk,self.a.pk])
        self.pr.resident=self.a;self.pr.save();self.cc.resident=self.a
        self.cc.starts_at+=timedelta(days=2);self.cc.ends_at+=timedelta(days=2);self.cc.save()
        self.pre.resident=self.b;self.pre.save()
        with self.assertRaisesMessage(ValidationError,'mesmo dia'):self.report()

    def test_duplicate_identifiers_or_missing_ids_are_rejected(self):
        for ids in [[],[self.pr.pk,self.pr.pk],['x'],[True],[self.pr.pk+1000]]:
            with self.subTest(ids=ids),self.assertRaises(ValidationError):review_bundle(self.a,self.b,ids,[self.pre.pk],'selected')
        with self.assertRaises(ValidationError):review_bundle(self.a,self.a,[self.pr.pk],[self.pre.pk])

    def test_failure_in_middle_rolls_back_every_transfer(self):
        proposal=self.proposal()
        from django.db.models.query import QuerySet
        update=QuerySet.update
        counter={'shift':0}
        def failing(query,*args,**kwargs):
            if query.model is Shift and 'resident_id' in kwargs:
                counter['shift']+=1
                if counter['shift']==2:raise RuntimeError('isolated failure')
            return update(query,*args,**kwargs)
        with patch.object(QuerySet,'update',failing):
            with self.assertRaises(RuntimeError):self.accept(proposal)
        self.assertEqual(self.owners(),[self.a.pk,self.a.pk,self.b.pk])
        proposal.refresh_from_db();self.assertEqual(proposal.status,'pending')
        self.assertIsNone(proposal.recipient_acknowledged_at)

    def test_duplicate_proposal_prevented_and_competing_requests_cancelled_after_accept(self):
        proposal=self.proposal('selected')
        with self.assertRaisesMessage(ValidationError,'igual'):self.proposal('selected')
        single=Exchange.objects.create(offered=self.pr,requested=self.pre,proposer=self.a,recipient=self.b)
        alternative=review_bundle(self.a,self.b,[self.cc.pk],[self.pre.pk],'selected')
        competing=propose_bundle(self.a,self.b,[self.cc.pk],[self.pre.pk],'selected',review_fingerprint=alternative['fingerprint'],acknowledged=True)
        self.accept(proposal)
        single.refresh_from_db();competing.refresh_from_db()
        self.assertEqual(single.status,'cancelled');self.assertEqual(competing.status,'cancelled')

    def test_pending_expiration_uses_explicit_clock(self):
        proposal=self.proposal()
        with self.assertRaisesMessage(ValidationError,'futuras'):
            review_pending_bundle(proposal,now=self.pre.starts_at+timedelta(minutes=1))

    def test_overnight_exchange_splits_daily_minutes_without_changing_selected_total(self):
        Shift.objects.filter(pk__in=[self.pr.pk,self.cc.pk]).delete()
        overnight=Shift.objects.create(external_id='bundle-overnight-regular',resident=self.a,
            starts_at=self.day.replace(hour=23),ends_at=(self.day+timedelta(days=1)).replace(hour=1),
            sector='Estágio que atravessa meia-noite')
        report=review_bundle(self.a,self.b,[overnight.pk],[self.pre.pk],'selected')
        first,following=self.day.date().isoformat(),(self.day+timedelta(days=1)).date().isoformat()
        a_days={item['date']:item for item in report['proposer']['days']}
        b_days={item['date']:item for item in report['recipient']['days']}
        self.assertEqual(report['proposer']['outgoing_minutes'],120)
        self.assertEqual(report['recipient']['incoming_minutes'],120)
        self.assertEqual((a_days[first]['before_minutes'],a_days[following]['before_minutes']),(60,60))
        self.assertEqual((a_days[first]['after_minutes'],a_days[following]['after_minutes']),(560,0))
        self.assertEqual((b_days[first]['after_minutes'],b_days[following]['after_minutes']),(60,60))
        self.assertEqual(sum(item['after_minutes'] for item in b_days.values()),120)

    def test_kept_previous_night_counts_only_after_local_midnight_and_excludes_boundaries(self):
        Shift.objects.create(external_id='bundle-kept-previous-night',resident=self.a,
            starts_at=(self.day-timedelta(days=1)).replace(hour=22),ends_at=self.day.replace(hour=2),
            sector='Compromisso da véspera',category='night',resident_group='R1')
        Shift.objects.create(external_id='bundle-ended-midnight',resident=self.a,
            starts_at=(self.day-timedelta(days=1)).replace(hour=20),ends_at=self.day,
            sector='Termina na fronteira',category='night',resident_group='R1')
        Shift.objects.create(external_id='bundle-starts-next-midnight',resident=self.a,
            starts_at=self.day+timedelta(days=1),ends_at=self.day+timedelta(days=1,hours=2),
            sector='Começa na fronteira seguinte',category='night',resident_group='R1')
        report=self.report()
        days=report['proposer']['days']
        self.assertEqual(len(days),1)
        self.assertEqual(days[0]['date'],self.day.date().isoformat())
        self.assertEqual(days[0]['before_minutes'],615+120)
        self.assertEqual(days[0]['after_minutes'],560+120)
        self.assertEqual(days[0]['before_label'],'12h15')
        self.assertEqual(report['proposer']['outgoing_minutes'],615)
        self.assertEqual(report['proposer']['incoming_minutes'],560)
