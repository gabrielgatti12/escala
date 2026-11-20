"""Reviewed direct exchanges of regular activities, including a two-for-one day.

Imported times are evidence to review, not a legal workload policy. Overlaps
and unequal hours are disclosed to both participants and require their explicit
acknowledgement. Identity, ownership, future dates and the reviewed terms remain
hard requirements.
"""
import hashlib
import json
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from itertools import combinations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .models import BundleExchange, BundleExchangeItem, Exchange, ExchangeTopic, IncompleteActivity, SharedActivity, Shift
from .presentation import sector_label
from .services import _require_active_resident_ids

MAX_ITEMS = 20


def _ids(values):
    try:
        if isinstance(values,(str,bytes,dict)):raise ValueError
        raw=list(values)
        if not 1 <= len(raw) <= MAX_ITEMS or any(isinstance(value,bool) for value in raw):raise ValueError
        result=[int(value) for value in raw]
        if len(set(result))!=len(result) or any(not 0<value<=9223372036854775807 for value in result):raise ValueError
        if any(str(number)!=str(original).strip() for number,original in zip(result,raw)):raise ValueError
        return sorted(result)
    except (TypeError,ValueError,OverflowError):
        raise ValidationError('Selecione de 1 a 20 atividades distintas de cada residente.')


def _bounds(day):
    return (timezone.make_aware(datetime.combine(day,time.min)),
            timezone.make_aware(datetime.combine(day+timedelta(days=1),time.min)))


def _duration(shift):
    return int((shift.ends_at-shift.starts_at).total_seconds()//60)


def _hours(minutes):
    sign='−' if minutes<0 else ''
    hours,rest=divmod(abs(minutes),60)
    return f'{sign}{hours}h{rest:02d}'


def _name(person):
    return person.get_full_name() or person.username


def _record(shift):
    start,end=timezone.localtime(shift.starts_at),timezone.localtime(shift.ends_at)
    return {'id':shift.pk,'resident_id':shift.resident_id,'resident_name':_name(shift.resident),
            'starts_at':start.isoformat(),'ends_at':end.isoformat(),'date':start.date().isoformat(),
            'date_label':start.strftime('%d/%m/%Y'),'start_label':start.strftime('%H:%M'),'end_label':end.strftime('%H:%M'),
            'sector':shift.sector,'display_sector':sector_label(shift.sector),'notes':shift.notes,
            'source':shift.source,'source_tab':shift.source_tab,'source_cell':shift.source_cell,
            'category':shift.category,'active':shift.active,'requires_reconciliation':shift.requires_reconciliation,
            'minutes':_duration(shift),'hours_label':_hours(_duration(shift))}


def _intersects(first,second):
    return first.starts_at<second.ends_at and second.starts_at<first.ends_at


def _windows(activities):
    days=set()
    for activity in activities:
        day=timezone.localdate(activity.starts_at)
        final=timezone.localdate(activity.ends_at-timedelta(microseconds=1))
        # An individual imported activity may cross midnight. Refuse malformed
        # spans instead of expanding an arbitrarily large interval into days.
        if (final-day).days>2:
            raise ValidationError('Uma atividade tem duração fora do esperado. Confira a origem antes de negociar.')
        while day<=final:
            days.add(day);day+=timedelta(days=1)
    return sorted(days)


def _person_context(person,outgoing,incoming,all_ids,lock=False):
    days=_windows(outgoing+incoming)
    query=Q(pk__in=[])
    for day in days:
        start,end=_bounds(day)
        query|=Q(starts_at__lt=end,ends_at__gt=start)
    kept=Shift.objects.filter(active=True,resident=person).exclude(pk__in=all_ids).filter(query).select_related('resident').order_by('starts_at','pk')
    if lock:kept=kept.select_for_update()
    kept=list(kept)
    shared=list(SharedActivity.objects.filter(active=True,allocated_to__isnull=True,residents=person).filter(query).prefetch_related('residents').order_by('starts_at','pk'))
    incomplete=list(IncompleteActivity.objects.filter(residents=person,date__in=days).prefetch_related('residents').order_by('date','pk'))
    outgoing_minutes=sum(_duration(item) for item in outgoing)
    incoming_minutes=sum(_duration(item) for item in incoming)
    delta=incoming_minutes-outgoing_minutes
    warnings=[]
    if delta:
        warnings.append({'code':'workload','resident_id':person.pk,'severity':'warning',
            'message':f'{_name(person)}: entrega {_hours(outgoing_minutes)} e assume {_hours(incoming_minutes)}; diferença de {_hours(delta)} pelos horários da planilha.'})
    # Show the precise conflicting intervals, including incoming activities that
    # conflict with one another. Acknowledgement never changes their times.
    comparisons=[(first,second) for first in incoming for second in kept]
    comparisons.extend(combinations(incoming,2))
    for first,second in comparisons:
        if _intersects(first,second):
            one,two=_record(first),_record(second)
            warnings.append({'code':'overlap','resident_id':person.pk,'severity':'warning',
                'first':one,'second':two,
                'message':f'{_name(person)}: {one["display_sector"]} ({one["date_label"]} {one["start_label"]}–{one["end_label"]}) se sobrepõe a {two["display_sector"]} ({two["date_label"]} {two["start_label"]}–{two["end_label"]}). Confiram os horários reais antes de concordar.'})
    transitions=set()
    schedule=kept+incoming
    for first in incoming:
        before=[item for item in schedule if item.pk!=first.pk and item.ends_at<=first.starts_at and timezone.localdate(item.ends_at)==timezone.localdate(first.starts_at)]
        after=[item for item in schedule if item.pk!=first.pk and item.starts_at>=first.ends_at and timezone.localdate(item.starts_at)==timezone.localdate(first.ends_at)]
        neighbors=[]
        if before:neighbors.append((max(before,key=lambda item:item.ends_at),first))
        if after:neighbors.append((first,min(after,key=lambda item:item.starts_at)))
        for previous,next_item in neighbors:
            if (previous.pk,next_item.pk) in transitions:continue
            transitions.add((previous.pk,next_item.pk))
            end,start=timezone.localtime(previous.ends_at),timezone.localtime(next_item.starts_at)
            gap=int((next_item.starts_at-previous.ends_at).total_seconds()//60)
            warnings.append({'code':'transition','resident_id':person.pk,'severity':'info',
                'message':f'{_name(person)}: {sector_label(previous.sector)} termina às {end:%H:%M} e {sector_label(next_item.sector)} começa às {start:%H:%M} em {start:%d/%m/%Y}; intervalo de {_hours(gap)}.'})
    if shared or incomplete:
        warnings.append({'code':'undefined','resident_id':person.pk,'severity':'warning',
            'message':f'{_name(person)} ainda tem atividades a combinar ou sem horário nesses dias. A carga total pode mudar após a definição.'})
    days_report=[]
    for day in days:
        # Count only the part of each published interval inside this local day.
        # This includes commitments that began the previous evening and avoids
        # charging a whole overnight activity to its starting date. Overlapping
        # activities are still summed and separately disclosed in the warnings.
        start,end=_bounds(day)
        def minutes_in_day(item):
            first=max(item.starts_at,start)
            last=min(item.ends_at,end)
            if last<=first:return 0
            duration=last.astimezone(datetime_timezone.utc)-first.astimezone(datetime_timezone.utc)
            return int(duration.total_seconds()//60)
        before_minutes=sum(minutes_in_day(item) for item in kept+outgoing)
        after_minutes=sum(minutes_in_day(item) for item in kept+incoming)
        days_report.append({'date':day.isoformat(),'date_label':day.strftime('%d/%m/%Y'),
                            'before_minutes':before_minutes,'after_minutes':after_minutes,
                            'before_label':_hours(before_minutes),'after_label':_hours(after_minutes)})
    return {'resident_id':person.pk,'name':_name(person),
            'outgoing_minutes':outgoing_minutes,'incoming_minutes':incoming_minutes,'delta_minutes':delta,
            'outgoing_label':_hours(outgoing_minutes),'incoming_label':_hours(incoming_minutes),'delta_label':('+' if delta>0 else '')+_hours(delta),
            'days':days_report,'warnings':warnings,'kept':[_record(item) for item in kept],
            'shared':[{'id':item.pk,'starts_at':item.starts_at.isoformat(),'ends_at':item.ends_at.isoformat(),
                       'sector':item.sector,'participants':sorted(person.pk for person in item.residents.all())} for item in shared],
            'incomplete':[{'id':item.pk,'date':item.date.isoformat(),'sector':item.sector,'raw_time':item.raw_time,
                           'participants':sorted(person.pk for person in item.residents.all())} for item in incomplete]}


def _review(proposer,recipient,offered_ids,requested_ids,mode,lock=False,now=None):
    _require_active_resident_ids(proposer.pk,recipient.pk)
    if proposer.pk==recipient.pk:raise ValidationError('Escolha outro residente para receber a proposta.')
    if mode not in ['day','selected']:raise ValidationError('Escolha troca do dia ou de atividades selecionadas.')
    offered_ids,requested_ids=_ids(offered_ids),_ids(requested_ids)
    if set(offered_ids)&set(requested_ids):raise ValidationError('Uma atividade não pode aparecer nos dois lados da troca.')
    all_ids=offered_ids+requested_ids
    query=Shift.objects.filter(pk__in=all_ids).select_related('resident').order_by('pk')
    if lock:query=query.select_for_update()
    shifts={item.pk:item for item in query}
    if len(shifts)!=len(all_ids):raise ValidationError('Uma das atividades não foi encontrada.')
    sides=[]
    now=now or timezone.now()
    for ids,person in [(offered_ids,proposer),(requested_ids,recipient)]:
        activities=sorted([shifts[pk] for pk in ids],key=lambda item:(item.starts_at,item.pk))
        if any(item.resident_id!=person.pk for item in activities):raise ValidationError('A titularidade de uma atividade mudou. Prepare outra proposta.')
        if any(not item.active or item.requires_reconciliation for item in activities):raise ValidationError('Uma atividade foi removida ou precisa de conciliação.')
        if any(item.category!='regular' for item in activities):raise ValidationError('A troca de conjuntos é somente para estágios. Plantões usam a troca individual com suas regras de grupo.')
        if any(item.starts_at<=now for item in activities):raise ValidationError('Só é possível negociar atividades futuras.')
        dates={timezone.localdate(item.starts_at) for item in activities}
        if len(dates)!=1:raise ValidationError('Cada lado da troca deve reunir atividades do mesmo dia.')
        day=next(iter(dates));start,end=_bounds(day)
        if mode=='day':
            complete=set(Shift.objects.filter(active=True,resident=person,category='regular',starts_at__gte=start,starts_at__lt=end).values_list('pk',flat=True))
            if complete!=set(ids):raise ValidationError('Na troca do dia inteiro, inclua todos os estágios daquele dia.')
            if SharedActivity.objects.filter(active=True,allocated_to__isnull=True,residents=person,starts_at__lt=end,ends_at__gt=start).exists() or IncompleteActivity.objects.filter(residents=person,date=day).exists():
                raise ValidationError('Este dia tem atividades a combinar ou sem horário. Defina-as antes de trocar o dia inteiro, ou escolha atividades específicas.')
        sides.append(activities)
    offered,requested=sides
    first=_person_context(proposer,offered,requested,all_ids,lock=lock)
    second=_person_context(recipient,requested,offered,all_ids,lock=lock)
    terms={'mode':mode,'proposer_id':proposer.pk,'recipient_id':recipient.pk,
           'offered':[_record(item) for item in offered],'requested':[_record(item) for item in requested],
           'proposer':first,'recipient':second}
    fingerprint=hashlib.sha256(json.dumps(terms,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return {**terms,'terms':terms,'fingerprint':fingerprint,'warnings':first['warnings']+second['warnings'],
            'earliest_start':min(item.starts_at for item in offered+requested).isoformat()}


def review_bundle(proposer,recipient,offered_ids,requested_ids,mode='day'):
    """Return a JSON-serializable reviewed report and deterministic fingerprint."""
    return _review(proposer,recipient,offered_ids,requested_ids,mode)


def review_pending_bundle(bundle,now=None):
    """Revalidate an unmodified pending proposal, raising on changed terms."""
    if bundle.status!='pending':raise ValidationError('Esta proposta já foi encerrada.')
    items=list(bundle.items.all())
    report=_review(bundle.proposer,bundle.recipient,
                         [item.shift_id for item in items if item.side=='offered'],
                         [item.shift_id for item in items if item.side=='requested'],bundle.mode,now=now)
    if not constant_time_compare(report['fingerprint'],bundle.fingerprint):
        raise ValidationError('A escala ou os horários mudaram desde a proposta. Cancele e prepare uma nova conferência para os dois.')
    return report


def _acknowledge(report,fingerprint,acknowledged):
    if acknowledged is not True or not isinstance(fingerprint,str) or not constant_time_compare(report['fingerprint'],fingerprint):
        raise ValidationError('Confira todas as atividades, horários e avisos e confirme sua concordância antes de continuar.')


@transaction.atomic
def propose_bundle(proposer,recipient,offered_ids,requested_ids,mode='day',review_fingerprint=None,acknowledged=False):
    report=_review(proposer,recipient,offered_ids,requested_ids,mode,lock=True)
    _acknowledge(report,review_fingerprint,acknowledged)
    if BundleExchange.objects.filter(proposer=proposer,recipient=recipient,status='pending',fingerprint=report['fingerprint']).exists():
        raise ValidationError('Já existe uma proposta igual aguardando o colega.')
    proposal=BundleExchange.objects.create(proposer=proposer,recipient=recipient,mode=mode,
        snapshot=report,fingerprint=report['fingerprint'],proposer_acknowledged_at=timezone.now())
    BundleExchangeItem.objects.bulk_create([
        BundleExchangeItem(exchange=proposal,shift_id=record['id'],side=side,original_owner_id=owner.pk)
        for records,side,owner in [(report['offered'],'offered',proposer),(report['requested'],'requested',recipient)]
        for record in records
    ])
    from .reminders import notify_request_created
    notify_request_created(proposal)
    return proposal


@transaction.atomic
def respond_bundle(actor,pk,action,review_fingerprint=None,acknowledged=False):
    bundle=BundleExchange.objects.select_for_update().select_related('proposer','recipient').get(pk=pk)
    _require_active_resident_ids(actor.pk,bundle.proposer_id,bundle.recipient_id)
    if bundle.status!='pending':raise ValidationError('Esta proposta já foi encerrada.')
    if action=='cancel' and actor.pk==bundle.proposer_id:
        bundle.status='cancelled'
    elif action in ['accept','reject'] and actor.pk==bundle.recipient_id:
        if action=='accept':
            items=list(bundle.items.all())
            report=_review(bundle.proposer,bundle.recipient,
                [item.shift_id for item in items if item.side=='offered'],
                [item.shift_id for item in items if item.side=='requested'],bundle.mode,lock=True)
            if not constant_time_compare(report['fingerprint'],bundle.fingerprint):
                raise ValidationError('A escala ou os horários mudaram desde a proposta. Prepare uma nova conferência para os dois.')
            _acknowledge(report,review_fingerprint,acknowledged)
            ids=[item.shift_id for item in items]
            for item in items:
                expected=bundle.proposer_id if item.side=='offered' else bundle.recipient_id
                if item.original_owner_id!=expected:raise ValidationError('Os participantes da proposta mudaram. Prepare outra proposta.')
                owner=bundle.recipient_id if item.side=='offered' else bundle.proposer_id
                Shift.objects.filter(pk=item.shift_id).update(resident_id=owner,wants_exchange=False)
            ExchangeTopic.objects.filter(shift_id__in=ids,closed_at__isnull=True).update(closed_at=timezone.now())
            Exchange.objects.filter(status='pending').filter(Q(offered_id__in=ids)|Q(requested_id__in=ids)).update(status='cancelled',resolved_at=timezone.now())
            from .bundle_integrations import cancel_pending_bundles
            cancel_pending_bundles(ids,exclude_pk=bundle.pk)
            bundle.recipient_acknowledged_at=timezone.now()
        bundle.status='accepted' if action=='accept' else 'rejected'
    else:raise ValidationError('Somente o colega destinatário pode aceitar ou recusar esta proposta.')
    bundle.resolved_at=timezone.now()
    bundle.save(update_fields=['status','resolved_at','recipient_acknowledged_at'])
    from .reminders import close_request_notifications
    close_request_notifications(bundle,now=bundle.resolved_at)
    return bundle
