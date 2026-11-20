"""Private resident-to-resident day/activity proposals with two explicit reviews."""
from datetime import datetime, time, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .bundle_exchanges import review_bundle, review_pending_bundle, propose_bundle, respond_bundle
from .identity import resident_for_account
from .models import BundleExchange, Shift

SALT='schedule.bundle-reviewed-proposal'


def recipients(person):
    future=Shift.objects.filter(resident_id=OuterRef('pk'),active=True,category='regular',starts_at__gt=timezone.now())
    return get_user_model().objects.filter(is_active=True).exclude(pk=person.pk).annotate(has_stages=Exists(future)).filter(has_stages=True).order_by('first_name','username')


class PickForm(forms.Form):
    recipient=forms.ModelChoiceField(queryset=get_user_model().objects.none(),label='Enviar diretamente para',empty_label='Escolha o residente')
    offered_day=forms.DateField(label='Dia dos meus estágios',widget=forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'))
    requested_day=forms.DateField(label='Dia dos estágios do colega',widget=forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'))
    mode=forms.ChoiceField(label='O que trocar',choices=[('day','Dia inteiro de estágios'),('selected','Só as atividades que eu escolher')])

    def __init__(self,*args,person,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['recipient'].queryset=recipients(person)
        self.fields['recipient'].label_from_instance=lambda item:item.get_full_name() or item.username

    def clean(self):
        data=super().clean()
        today=timezone.localdate()
        for field in ['offered_day','requested_day']:
            day=data.get(field)
            if day and (day.year in [1,9999] or day<today):self.add_error(field,'Escolha um dia atual ou futuro válido.')
        return data


def _day_shifts(person,day):
    start=timezone.make_aware(datetime.combine(day,time.min))
    end=timezone.make_aware(datetime.combine(day+timedelta(days=1),time.min))
    return list(Shift.objects.filter(active=True,category='regular',resident=person,starts_at__gte=start,starts_at__lt=end).order_by('starts_at','pk')[:21])


def _initial(request,person):
    today=timezone.localdate()
    initial={'offered_day':request.GET.get('offered_day',today.isoformat()),'requested_day':request.GET.get('requested_day',request.GET.get('offered_day',today.isoformat())),
             'mode':request.GET.get('mode','day'),'recipient':request.GET.get('recipient','')}
    return initial


def _pick_page(request,form,offered=None,requested=None):
    return render(request,'bundle_compose.html',{'tab':'requests','form':form,'offered':offered or [],'requested':requested or [],
        'loaded':form.is_bound and form.is_valid(),'now':timezone.now(),'resident_user':resident_for_account(request.user)})


@login_required
@require_GET
def compose(request):
    person=resident_for_account(request.user)
    initial=_initial(request,person)
    form=PickForm(initial if request.GET.get('load') else None,initial=initial,person=person)
    offered=requested=[]
    if form.is_bound and form.is_valid():
        data=form.cleaned_data
        offered=_day_shifts(person,data['offered_day']);requested=_day_shifts(data['recipient'],data['requested_day'])
    return _pick_page(request,form,offered,requested)


@login_required
@require_POST
def prepare(request):
    person=resident_for_account(request.user)
    form=PickForm(request.POST,person=person)
    if not form.is_valid():return _pick_page(request,form)
    data=form.cleaned_data
    offered=_day_shifts(person,data['offered_day']);requested=_day_shifts(data['recipient'],data['requested_day'])
    try:
        if data['mode']=='day':
            offered_ids=[item.pk for item in offered];requested_ids=[item.pk for item in requested]
        else:
            offered_ids=request.POST.getlist('offered');requested_ids=request.POST.getlist('requested')
            # A crafted form cannot silently change the reviewed dates.
            if not set(offered_ids)<=set(str(item.pk) for item in offered) or not set(requested_ids)<=set(str(item.pk) for item in requested):
                raise ValidationError('Selecione atividades dos dias exibidos. Carregue novamente os dias desejados.')
        report=review_bundle(person,data['recipient'],offered_ids,requested_ids,data['mode'])
    except ValidationError as error:
        messages.error(request,'; '.join(error.messages));return _pick_page(request,form,offered,requested)
    payload={'proposer':person.pk,'recipient':data['recipient'].pk,'offered':[item['id'] for item in report['offered']],
        'requested':[item['id'] for item in report['requested']],'mode':data['mode'],'fingerprint':report['fingerprint'],'warnings':bool(report['warnings'])}
    token=signing.dumps(payload,salt=SALT,compress=True)
    return render(request,'bundle_review.html',{'tab':'requests','report':report,'review_token':token,'stage':'propose','resident_user':person})


@login_required
@require_POST
def confirm(request):
    person=resident_for_account(request.user)
    try:
        payload=signing.loads(request.POST.get('review_token',''),salt=SALT,max_age=900)
        if payload['proposer']!=person.pk:raise signing.BadSignature
        acknowledged=request.POST.get('acknowledged')=='yes' and (not payload['warnings'] or request.POST.get('warnings_acknowledged')=='yes')
        recipient=get_object_or_404(get_user_model(),pk=payload['recipient'],is_active=True)
        proposal=propose_bundle(person,recipient,payload['offered'],payload['requested'],payload['mode'],payload['fingerprint'],acknowledged)
    except (signing.BadSignature,KeyError,TypeError,ValueError):
        messages.error(request,'A conferência expirou ou não corresponde a esta conta. Prepare outra proposta.');return redirect('bundle_compose')
    except ValidationError as error:
        messages.error(request,'; '.join(error.messages));return redirect('bundle_compose')
    messages.success(request,'Proposta enviada diretamente ao residente. A escala só muda após a conferência e o aceite dele.')
    return redirect('bundle_detail',pk=proposal.pk)


def _owned(person,pk):
    return get_object_or_404(BundleExchange.objects.filter(Q(proposer=person)|Q(recipient=person)).select_related('proposer','recipient').prefetch_related('items__shift'),pk=pk)


@login_required
@require_GET
def detail(request,pk):
    person=resident_for_account(request.user);proposal=_owned(person,pk)
    report=proposal.snapshot;problem=''
    if proposal.status=='pending':
        try:report=review_pending_bundle(proposal)
        except ValidationError as error:problem='; '.join(error.messages)
    return render(request,'bundle_review.html',{'tab':'requests','report':report,'bundle':proposal,'stage':'respond',
        'problem':problem,'can_accept':proposal.status=='pending' and not problem and proposal.recipient_id==person.pk,
        'resident_user':person})


@login_required
@require_POST
def respond(request,pk,action):
    person=resident_for_account(request.user);proposal=_owned(person,pk)
    acknowledged=request.POST.get('acknowledged')=='yes' and (not proposal.snapshot.get('warnings') or request.POST.get('warnings_acknowledged')=='yes')
    try:
        respond_bundle(person,pk,action,review_fingerprint=request.POST.get('fingerprint'),acknowledged=acknowledged)
        messages.success(request,'Troca completa confirmada. Os dois calendários já refletem a decisão.' if action=='accept' else 'Solicitação atualizada.')
    except ValidationError as error:messages.error(request,'; '.join(error.messages))
    return redirect('bundle_detail',pk=pk)
