"""Private in-app notices for requests; no email or external delivery."""
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Allocation, Exchange, Notification, Shift


REMINDER_INTERVAL = timedelta(hours=24)
REQUEST_LINK = '/?tab=requests&box=received'


def _request_type(proposal):
    return 'exchange' if isinstance(proposal, Exchange) else 'allocation'


def _keys(proposal):
    suffix = f'{_request_type(proposal)}:{proposal.pk}'
    return f'request:{suffix}', f'reminder:{suffix}'


def _is_pending_valid(proposal, now):
    if (proposal.status != 'pending' or proposal.proposer_id == proposal.recipient_id
            or not proposal.proposer.is_active or not proposal.recipient.is_active):
        return False
    # Reuse the same ownership, timing and overlap rules as confirmation.
    from .services import validate_allocation, validate_pair
    try:
        if isinstance(proposal, Exchange):
            validate_pair(proposal.offered, proposal.requested, proposal.proposer, proposal.recipient, now=now)
        else:
            recipient_id = validate_allocation(proposal.proposer, proposal.mine, proposal.theirs, now=now)
            if recipient_id != proposal.recipient_id:
                return False
            for activity, owner_id in [(proposal.mine, proposal.proposer_id), (proposal.theirs, proposal.recipient_id)]:
                if Shift.objects.filter(active=True, resident_id=owner_id, starts_at__lt=activity.ends_at, ends_at__gt=activity.starts_at).exists():
                    return False
    except ValidationError:
        return False
    return True


def _payload(proposal, now):
    return {
        'request_type': _request_type(proposal),
        'request_id': proposal.pk,
        'status': 'pending',
        'notified_at': now.isoformat(),
    }


def _message(proposal):
    proposer = proposal.proposer.get_full_name() or proposal.proposer.username
    if isinstance(proposal, Exchange):
        offered = proposal.offered
        requested = proposal.requested
        return (
            f'{proposer} propôs uma troca: '
            f'{timezone.localtime(offered.starts_at):%d/%m/%Y %H:%M} · {offered.sector} '
            f'por {timezone.localtime(requested.starts_at):%d/%m/%Y %H:%M} · {requested.sector}. '
            'Confira a proposta para aceitar ou recusar.'
        )
    return (
        f'{proposer} propôs dividir as atividades de '
        f'{timezone.localtime(proposal.mine.starts_at):%d/%m/%Y}: '
        f'{proposal.mine.sector} e {proposal.theirs.sector}. '
        'Confira a proposta para aceitar ou recusar.'
    )


@transaction.atomic
def notify_request_created(proposal, now=None):
    """Return (notice, created), always addressed to the resident identity."""
    now = now or timezone.now()
    if not _is_pending_valid(proposal, now):
        return None, False
    return _request_notice(proposal, now)


def _request_notice(proposal, now):
    key, _ = _keys(proposal)
    return Notification.objects.get_or_create(
        dedup_key=key,
        defaults={
            'resident_id': proposal.recipient_id,
            'kind': 'request',
            'title': 'Nova solicitação de troca' if isinstance(proposal, Exchange) else 'Nova proposta de divisão',
            'message': _message(proposal),
            'payload': _payload(proposal, now),
            'link': REQUEST_LINK,
        },
    )


@transaction.atomic
def close_request_notifications(proposal, now=None):
    """Retain request history while removing obsolete unread prompts."""
    now = now or timezone.now()
    count = 0
    for notice in Notification.objects.select_for_update().filter(dedup_key__in=_keys(proposal)):
        payload = dict(notice.payload)
        payload['status'] = proposal.status if proposal.status != 'pending' else 'closed'
        fields = ['payload']
        notice.payload = payload
        if notice.read_at is None:
            notice.read_at = now
            fields.append('read_at')
            count += 1
        notice.save(update_fields=fields)
    return count


def _timestamp(value, fallback):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if timezone.is_aware(parsed) else fallback
    except (TypeError, ValueError):
        return fallback


@transaction.atomic
def refresh_request_notifications(now=None):
    """Create missing notices and reuse one daily reminder per pending request.

    Each request has at most two rows: the original notice and its latest
    reminder. An older request first receives only its original notice; its
    reminder starts 24 hours after that notice, avoiding a double alert.
    """
    now = now or timezone.now()
    counts = {'requests': 0, 'reminders': 0, 'closed': 0}
    valid_keys = set()
    exchanges = Exchange.objects.filter(status='pending', offered__starts_at__gt=now, requested__starts_at__gt=now).select_related('proposer', 'recipient', 'offered', 'requested')
    allocations = Allocation.objects.filter(status='pending', mine__starts_at__gt=now, theirs__starts_at__gt=now).select_related('proposer', 'recipient', 'mine', 'theirs').prefetch_related('mine__residents', 'theirs__residents')
    for proposals in [exchanges, allocations]:
        for proposal in proposals:
            if not _is_pending_valid(proposal, now):
                continue
            initial_key, reminder_key = _keys(proposal)
            valid_keys.update([initial_key, reminder_key])
            notice, created = _request_notice(proposal, now)
            if created:
                counts['requests'] += 1
                continue
            if notice is None:
                continue
            first_notice_at = _timestamp(notice.payload.get('notified_at'), notice.created_at)
            if now < max(proposal.created_at, first_notice_at) + REMINDER_INTERVAL:
                continue
            reminder = Notification.objects.select_for_update().filter(dedup_key=reminder_key).first()
            if reminder:
                last_reminded = _timestamp(reminder.payload.get('last_reminded_at'), reminder.created_at)
                if now < last_reminded + REMINDER_INTERVAL:
                    continue
                reminder.message = _message(proposal)
                reminder.payload = {**_payload(proposal, now), 'last_reminded_at': now.isoformat()}
                reminder.read_at = None
                reminder.save(update_fields=['message', 'payload', 'read_at'])
            else:
                _, reminder_created = Notification.objects.get_or_create(
                    dedup_key=reminder_key,
                    defaults={
                        'resident_id': proposal.recipient_id,
                        'kind': 'reminder',
                        'title': 'Solicitação aguardando sua resposta',
                        'message': _message(proposal),
                        'payload': {**_payload(proposal, now), 'last_reminded_at': now.isoformat()},
                        'link': REQUEST_LINK,
                    },
                )
                if not reminder_created:
                    continue
            counts['reminders'] += 1
    # Requests closed, expired or invalidated by a source update must stop
    # appearing as unread prompts even if no response view was used.
    stale = Notification.objects.filter(kind__in=['request', 'reminder']).exclude(dedup_key__in=valid_keys).exclude(payload__status__in=['closed', 'accepted', 'rejected', 'cancelled'])
    for notice in stale:
        if not notice.dedup_key or not notice.dedup_key.startswith(('request:exchange:', 'request:allocation:', 'reminder:exchange:', 'reminder:allocation:')):
            continue
        notice.payload = {**notice.payload, 'status': 'closed'}
        fields = ['payload']
        if notice.read_at is None:
            notice.read_at = now
            fields.append('read_at')
            counts['closed'] += 1
        notice.save(update_fields=fields)
    return counts
