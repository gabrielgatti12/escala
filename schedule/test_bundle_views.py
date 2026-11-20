from datetime import datetime, timedelta, time

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from .models import BundleExchange, ExchangeTopic, Shift, SharedActivity


class PrivateBundleViewTests(TestCase):
    def setUp(self):
        users=get_user_model()
        self.a=users.objects.create_user('bundle-view-a',first_name='Residente A')
        self.b=users.objects.create_user('bundle-view-b',first_name='Residente B')
        self.other=users.objects.create_user('bundle-view-other')
        day=timezone.localdate()+timedelta(days=8)
        self.day=day-timedelta(days=day.weekday())+timedelta(days=2)
        def make(key,person,start,end,sector):
            return Shift.objects.create(external_id=key,resident=person,
                starts_at=timezone.make_aware(datetime.combine(self.day,time(*start))),
                ends_at=timezone.make_aware(datetime.combine(self.day,time(*end))),sector=sector)
        self.pr=make('view-pr',self.a,(7,45),(12,0),'INSTITUTO · (PR) Projeto Retina')
        self.cc=make('view-cc',self.a,(13,0),(19,0),'HOSPITAL · CC Leonardo Pinheiro')
        self.pre=make('view-pre',self.b,(7,40),(17,0),'INSTITUTO · Pré-Operatório')
        self.client.force_login(self.a)
        self.data={'recipient':self.b.pk,'offered_day':self.day.isoformat(),'requested_day':self.day.isoformat(),'mode':'day'}

    def prepare(self,**changes):
        return self.client.post(reverse('bundle_prepare'),{**self.data,**changes})

    def propose(self):
        response=self.prepare();token=response.context['review_token']
        return self.client.post(reverse('bundle_confirm'),{'review_token':token,'acknowledged':'yes','warnings_acknowledged':'yes'})

    def test_load_lists_only_two_selected_days_and_preview_shows_real_example(self):
        loaded=self.client.get(reverse('bundle_compose'),{**self.data,'load':'1'})
        self.assertEqual([i.pk for i in loaded.context['offered']],[self.pr.pk,self.cc.pk])
        self.assertEqual([i.pk for i in loaded.context['requested']],[self.pre.pk])
        response=self.prepare()
        for text in ['10h15','9h20','0h55','07:45','12:00','13:00','19:00','07:40','17:00']:
            self.assertContains(response,text)
        self.assertFalse(BundleExchange.objects.exists())
        self.assertEqual(Shift.objects.filter(resident=self.a).count(),2)

    def test_explicit_acknowledgements_required_by_both_and_three_shifts_move_together(self):
        response=self.prepare();token=response.context['review_token']
        for data in [{'review_token':token},{'review_token':token,'acknowledged':'yes'}]:
            self.client.post(reverse('bundle_confirm'),data)
            self.assertFalse(BundleExchange.objects.exists())
        self.propose();proposal=BundleExchange.objects.get()
        self.assertEqual(proposal.status,'pending')
        self.assertFalse(ExchangeTopic.objects.exists())
        self.assertEqual(Shift.objects.filter(resident=self.a).count(),2)
        self.client.force_login(self.b)
        page=self.client.get(reverse('bundle_detail',args=[proposal.pk]))
        self.assertContains(page,'Conferi e aceito a troca completa')
        url=reverse('bundle_respond',args=[proposal.pk,'accept'])
        self.client.post(url,{'fingerprint':proposal.fingerprint,'acknowledged':'yes'})
        proposal.refresh_from_db();self.assertEqual(proposal.status,'pending')
        self.client.post(url,{'fingerprint':proposal.fingerprint,'acknowledged':'yes','warnings_acknowledged':'yes'})
        proposal.refresh_from_db();self.assertEqual(proposal.status,'accepted')
        self.assertIsNotNone(proposal.proposer_acknowledged_at);self.assertIsNotNone(proposal.recipient_acknowledged_at)
        self.pr.refresh_from_db();self.cc.refresh_from_db();self.pre.refresh_from_db()
        self.assertEqual((self.pr.resident_id,self.cc.resident_id,self.pre.resident_id),(self.b.pk,self.b.pk,self.a.pk))
        self.assertEqual(Shift.objects.count(),3)

    def test_direct_proposal_is_private_and_senders_cannot_accept_for_recipient(self):
        self.propose();proposal=BundleExchange.objects.get()
        self.assertNotContains(self.client.get(reverse('bundle_detail',args=[proposal.pk])),'Conferi e aceito a troca completa')
        self.client.post(reverse('bundle_respond',args=[proposal.pk,'accept']),{'fingerprint':proposal.fingerprint,'acknowledged':'yes','warnings_acknowledged':'yes'})
        proposal.refresh_from_db();self.assertEqual(proposal.status,'pending')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('bundle_detail',args=[proposal.pk])).status_code,404)
        self.assertEqual(self.client.post(reverse('bundle_respond',args=[proposal.pk,'accept']),{}).status_code,404)
        self.assertNotContains(self.client.get('/?tab=requests'),f'href="{reverse("bundle_detail",args=[proposal.pk])}"')

    def test_changed_terms_block_acceptance_and_asks_for_new_conference(self):
        self.propose();proposal=BundleExchange.objects.get()
        Shift.objects.filter(pk=self.cc.pk).update(ends_at=self.cc.ends_at+timedelta(hours=1))
        self.client.force_login(self.b)
        page=self.client.get(reverse('bundle_detail',args=[proposal.pk]))
        self.assertContains(page,'mudaram desde a proposta')
        self.assertNotContains(page,'Conferi e aceito a troca completa')
        self.client.post(reverse('bundle_respond',args=[proposal.pk,'accept']),{'fingerprint':proposal.fingerprint,'acknowledged':'yes','warnings_acknowledged':'yes'})
        proposal.refresh_from_db();self.assertEqual(proposal.status,'pending')
        self.assertEqual(Shift.objects.filter(resident=self.a).count(),2)

    def test_selected_stage_discloses_overlap_with_kept_stage(self):
        response=self.prepare(mode='selected',offered=[self.pr.pk],requested=[self.pre.pk])
        self.assertContains(response,'se sobrepõe')
        self.assertContains(response,'CC Leonardo Pinheiro')
        self.assertContains(response,'13:00–19:00')
        self.assertContains(response,'07:40–17:00')
        self.assertEqual([i['id'] for i in response.context['report']['offered']],[self.pr.pk])

    def test_unsigned_foreign_or_expired_review_cannot_send(self):
        response=self.prepare();token=response.context['review_token']
        self.client.post(reverse('bundle_confirm'),{'review_token':token+'tampered','acknowledged':'yes','warnings_acknowledged':'yes'})
        self.assertFalse(BundleExchange.objects.exists())
        self.client.force_login(self.other)
        self.client.post(reverse('bundle_confirm'),{'review_token':token,'acknowledged':'yes','warnings_acknowledged':'yes'})
        self.assertFalse(BundleExchange.objects.exists())

    def test_csrf_auth_and_nonmutating_gets(self):
        self.propose();proposal=BundleExchange.objects.get()
        self.client.logout()
        self.assertEqual(self.client.get(reverse('bundle_compose')).status_code,302)
        strict=Client(enforce_csrf_checks=True);strict.force_login(self.b)
        self.assertEqual(strict.post(reverse('bundle_respond',args=[proposal.pk,'accept']),{}).status_code,403)
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(reverse('bundle_prepare')).status_code,405)
        self.assertEqual(self.client.get(reverse('bundle_confirm')).status_code,405)
        self.assertEqual(self.client.get(reverse('bundle_respond',args=[proposal.pk,'cancel'])).status_code,405)
        proposal.refresh_from_db();self.assertEqual(proposal.status,'pending')

    def test_inbox_counts_bundle_once_and_has_review_not_inline_accept(self):
        self.propose();proposal=BundleExchange.objects.get()
        self.client.force_login(self.b)
        page=self.client.get('/?tab=requests')
        self.assertEqual(page.context['received_count'],1)
        self.assertEqual(list(page.context['bundles']),[proposal])
        self.assertContains(page,'Revisar horários e responder')
        self.assertNotContains(page,'Conferi e aceito a troca completa')
        self.client.force_login(self.a)
        page=self.client.get('/?tab=requests&box=sent')
        self.assertEqual(page.context['sent_count'],1)
        self.assertEqual(list(page.context['bundles']),[proposal])

    def test_day_with_undefined_activity_needs_resolution_or_explicit_selection(self):
        shared=SharedActivity.objects.create(external_id='view-shared',source='fixture',starts_at=self.pr.starts_at,ends_at=self.pr.ends_at,sector='A combinar')
        shared.residents.set([self.a,self.b])
        page=self.prepare();self.assertContains(page,'Defina-as antes de trocar o dia inteiro')
        self.assertNotIn('review_token',page.context)
        page=self.prepare(mode='selected',offered=[self.pr.pk],requested=[self.pre.pk])
        self.assertIn('review_token',page.context)
        self.assertContains(page,'A carga total pode mudar',html=False)

    def test_individual_exchange_acceptance_cancels_overlapping_bundle(self):
        from .services import propose_exchange,respond_exchange
        self.propose();proposal=BundleExchange.objects.get()
        shift=Shift.objects.create(external_id='view-third-requested',resident=self.other,
            starts_at=self.pr.starts_at+timedelta(days=1),ends_at=self.pr.ends_at+timedelta(days=1),sector='Outro dia')
        single=propose_exchange(self.a,self.pr.pk,shift.pk)
        respond_exchange(self.other,single.pk,'accept')
        proposal.refresh_from_db();self.assertEqual(proposal.status,'cancelled')
        self.cc.refresh_from_db();self.pre.refresh_from_db()
        self.assertEqual((self.cc.resident_id,self.pre.resident_id),(self.a.pk,self.b.pk))

    def test_existing_worker_maintenance_expires_started_bundle(self):
        from .services import expire_pending_requests
        self.propose();proposal=BundleExchange.objects.get()
        Shift.objects.filter(pk=self.pr.pk).update(starts_at=timezone.now()-timedelta(hours=1))
        counts=expire_pending_requests()
        self.assertEqual(counts['bundles'],1)
        proposal.refresh_from_db();self.assertEqual(proposal.status,'cancelled')

    def test_review_token_expiration_requires_a_new_conference(self):
        import time
        from unittest.mock import patch
        token=self.prepare().context['review_token']
        with patch('django.core.signing.time.time',return_value=time.time()+1000):
            self.client.post(reverse('bundle_confirm'),{'review_token':token,'acknowledged':'yes','warnings_acknowledged':'yes'})
        self.assertFalse(BundleExchange.objects.exists())
