"""Keep accepted bundle decisions authoritative across reads and request maintenance."""
from collections import defaultdict

from django.db import transaction
from django.utils import timezone


def accepted_bundle_shift_ids(source=None):
    from .models import BundleExchangeItem
    rows=BundleExchangeItem.objects.filter(exchange__status='accepted')
    if source is not None:rows=rows.filter(shift__source=source)
    return set(rows.values_list('shift_id',flat=True))


def bundle_former_owners(source=None):
    from .models import BundleExchangeItem
    rows=BundleExchangeItem.objects.filter(exchange__status='accepted')
    if source is not None:rows=rows.filter(shift__source=source)
    result=defaultdict(set)
    for shift_id,owner_id in rows.values_list('shift_id','original_owner_id'):
        result[shift_id].add(owner_id)
    return result


@transaction.atomic
def cancel_pending_bundles(shift_ids,now=None,exclude_pk=None):
    from .models import BundleExchange,BundleExchangeItem
    from .reminders import close_request_notifications
    shift_ids=set(shift_ids)
    if not shift_ids:return 0
    now=now or timezone.now()
    ids=BundleExchangeItem.objects.filter(shift_id__in=shift_ids).values('exchange_id')
    rows=BundleExchange.objects.select_for_update().filter(pk__in=ids,status='pending')
    if exclude_pk is not None:rows=rows.exclude(pk=exclude_pk)
    count=0
    for proposal in rows:
        proposal.status='cancelled';proposal.resolved_at=now
        proposal.save(update_fields=['status','resolved_at'])
        close_request_notifications(proposal,now=now)
        count+=1
    return count


@transaction.atomic
def expire_pending_bundles(now=None):
    from .models import BundleExchange,BundleExchangeItem
    from .reminders import close_request_notifications
    now=now or timezone.now()
    expired_ids=BundleExchangeItem.objects.filter(shift__starts_at__lte=now).values('exchange_id')
    count=0
    for proposal in BundleExchange.objects.select_for_update().filter(status='pending',pk__in=expired_ids):
        proposal.status='cancelled';proposal.resolved_at=now
        proposal.save(update_fields=['status','resolved_at'])
        close_request_notifications(proposal,now=now)
        count+=1
    return count
