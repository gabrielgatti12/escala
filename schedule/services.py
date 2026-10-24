from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Shift, Exchange

def validate_pair(offered, requested, proposer, recipient):
    if not offered.active or not requested.active:
        raise ValidationError('Um dos plantões foi removido da planilha.')
    if offered.pk == requested.pk or proposer.pk == recipient.pk:
        raise ValidationError('Escolha um plantão de outro residente.')
    if offered.resident_id != proposer.pk or requested.resident_id != recipient.pk:
        raise ValidationError('A titularidade dos plantões mudou. Crie uma nova solicitação.')
    if min(offered.starts_at, requested.starts_at) <= timezone.now():
        raise ValidationError('Só é possível trocar plantões futuros.')
    for resident, incoming in [(proposer, requested), (recipient, offered)]:
        if Shift.objects.filter(active=True,resident=resident,starts_at__lt=incoming.ends_at,ends_at__gt=incoming.starts_at).exclude(pk__in=[offered.pk,requested.pk]).exists():
            raise ValidationError('A troca causaria conflito de horários.')

@transaction.atomic
def propose_exchange(user, offered_id, requested_id):
    # Lock in deterministic order; SQLite serializes writes and PostgreSQL locks rows.
    shifts = {s.pk:s for s in Shift.objects.select_for_update().filter(pk__in=[offered_id,requested_id]).order_by('pk')}
    if offered_id not in shifts or requested_id not in shifts:
        raise ValidationError('Plantão não encontrado.')
    offered, requested = shifts[offered_id], shifts[requested_id]
    validate_pair(offered,requested,user,requested.resident)
    if Exchange.objects.filter(offered=offered,requested=requested,status='pending').exists():
        raise ValidationError('Já existe uma solicitação pendente para esta troca.')
    return Exchange.objects.create(offered=offered,requested=requested,proposer=user,recipient=requested.resident)

@transaction.atomic
def respond_exchange(user, pk, action):
    exchange = Exchange.objects.select_for_update().get(pk=pk)
    if exchange.status != 'pending':
        raise ValidationError('Esta solicitação já foi encerrada.')
    if action == 'cancel' and exchange.proposer_id == user.pk:
        exchange.status = 'cancelled'
    elif action in ['accept','reject'] and exchange.recipient_id == user.pk:
        if action == 'accept':
            shifts = {s.pk:s for s in Shift.objects.select_for_update().filter(pk__in=[exchange.offered_id,exchange.requested_id]).order_by('pk')}
            offered, requested = shifts[exchange.offered_id], shifts[exchange.requested_id]
            validate_pair(offered,requested,exchange.proposer,exchange.recipient)
            offered.resident_id, requested.resident_id = exchange.recipient_id, exchange.proposer_id
            offered.wants_exchange = requested.wants_exchange = False
            offered.save(update_fields=['resident','wants_exchange'])
            requested.save(update_fields=['resident','wants_exchange'])
            ExchangeTopic.objects.filter(shift_id__in=shifts,closed_at__isnull=True).update(closed_at=timezone.now())
            Exchange.objects.filter(status='pending').exclude(pk=pk).filter(Q(offered_id__in=shifts)|Q(requested_id__in=shifts)).update(status='cancelled',resolved_at=timezone.now())
        exchange.status = 'accepted' if action == 'accept' else 'rejected'
    else:
        raise ValidationError('Você não pode realizar esta ação.')
    exchange.resolved_at = timezone.now()
    exchange.save(update_fields=['status','resolved_at'])
    return exchange

from .models import Allocation, SharedActivity

def validate_allocation(user,mine,theirs):
    if mine.pk==theirs.pk or mine.starts_at.date()!=theirs.starts_at.date():
        raise ValidationError('Escolha duas atividades distintas do mesmo dia.')
    participants=set(mine.residents.values_list('pk',flat=True))
    if len(participants)!=2 or user.pk not in participants or participants!=set(theirs.residents.values_list('pk',flat=True)):
        raise ValidationError('As duas atividades devem pertencer à mesma dupla.')
    if any(not a.active or a.allocated_to_id for a in [mine,theirs]):
        raise ValidationError('Uma das atividades já foi definida ou removida.')
    return next(pk for pk in participants if pk!=user.pk)

@transaction.atomic
def propose_allocation(user,mine_id,theirs_id):
    activities={a.pk:a for a in SharedActivity.objects.select_for_update().filter(pk__in=[mine_id,theirs_id]).order_by('pk')}
    if mine_id not in activities or theirs_id not in activities:
        raise ValidationError('Atividade não encontrada.')
    mine,theirs=activities[mine_id],activities[theirs_id]
    recipient_id=validate_allocation(user,mine,theirs)
    if Allocation.objects.filter(mine=mine,theirs=theirs,proposer=user,status='pending').exists():
        raise ValidationError('Esta proposta já está pendente.')
    return Allocation.objects.create(mine=mine,theirs=theirs,proposer=user,recipient_id=recipient_id)

@transaction.atomic
def respond_allocation(user,pk,action):
    allocation=Allocation.objects.select_for_update().get(pk=pk)
    if allocation.status!='pending': raise ValidationError('Proposta já encerrada.')
    if action=='cancel' and allocation.proposer_id==user.pk:
        allocation.status='cancelled'
    elif action in ['accept','reject'] and allocation.recipient_id==user.pk:
        if action=='accept':
            activities={a.pk:a for a in SharedActivity.objects.select_for_update().filter(pk__in=[allocation.mine_id,allocation.theirs_id]).order_by('pk')}
            mine,theirs=activities[allocation.mine_id],activities[allocation.theirs_id]
            validate_allocation(allocation.proposer,mine,theirs)
            for activity,owner_id in [(mine,allocation.proposer_id),(theirs,allocation.recipient_id)]:
                if Shift.objects.filter(active=True,resident_id=owner_id,starts_at__lt=activity.ends_at,ends_at__gt=activity.starts_at).exists():
                    raise ValidationError('A divisão causaria conflito com um plantão já confirmado.')
                activity.allocated_to_id=owner_id
                activity.save(update_fields=['allocated_to'])
                Shift.objects.create(external_id=f'allocation:{allocation.pk}:{activity.pk}',resident_id=owner_id,starts_at=activity.starts_at,ends_at=activity.ends_at,sector=activity.sector)
            Allocation.objects.filter(status='pending').exclude(pk=pk).filter(Q(mine_id__in=activities)|Q(theirs_id__in=activities)).update(status='cancelled',resolved_at=timezone.now())
        allocation.status='accepted' if action=='accept' else 'rejected'
    else: raise ValidationError('Você não pode realizar esta ação.')
    allocation.resolved_at=timezone.now()
    allocation.save(update_fields=['status','resolved_at'])
    return allocation

from .models import ExchangeTopic, ForumComment

@transaction.atomic
def set_exchange_interest(user,shift_id,desired=None):
    shift=Shift.objects.select_for_update().get(pk=shift_id)
    if shift.resident_id!=user.pk:
        raise ValidationError('Somente o titular pode publicar este plantão.')
    if not shift.active or shift.starts_at<=timezone.now():
        raise ValidationError('Somente plantões futuros e definidos podem ser publicados.')
    publish=not shift.wants_exchange if desired is None else desired
    topic=None
    if publish:
        ExchangeTopic.objects.filter(shift=shift,closed_at__isnull=True).exclude(poster=user).update(closed_at=timezone.now())
        topic,_=ExchangeTopic.objects.get_or_create(shift=shift,closed_at__isnull=True,defaults={'poster':user})
    else:
        ExchangeTopic.objects.filter(shift=shift,closed_at__isnull=True).update(closed_at=timezone.now())
    shift.wants_exchange=publish
    shift.save(update_fields=['wants_exchange'])
    return topic

@transaction.atomic
def add_forum_comment(user,topic_id,text):
    topic=ExchangeTopic.objects.get(pk=topic_id)
    shift=Shift.objects.select_for_update().get(pk=topic.shift_id)
    topic=ExchangeTopic.objects.select_for_update().get(pk=topic_id)
    topic.shift=shift
    if not topic.is_open:
        raise ValidationError('Esta publicação está encerrada ou o plantão não está mais disponível.')
    text=text.strip()
    if not text or len(text)>600:
        raise ValidationError('Escreva um comentário entre 1 e 600 caracteres.')
    return ForumComment.objects.create(topic=topic,author=user,text=text)

@transaction.atomic
def withdraw_forum_topic(user,topic_id):
    topic=ExchangeTopic.objects.get(pk=topic_id)
    shift=Shift.objects.select_for_update().get(pk=topic.shift_id)
    topic=ExchangeTopic.objects.select_for_update().get(pk=topic_id)
    if topic.poster_id!=user.pk:
        raise ValidationError('Somente o autor pode retirar esta publicação.')
    if topic.closed_at is not None: return topic
    if shift.resident_id==user.pk:
        shift.wants_exchange=False
        shift.save(update_fields=['wants_exchange'])
    topic.closed_at=timezone.now()
    topic.save(update_fields=['closed_at'])
    return topic
