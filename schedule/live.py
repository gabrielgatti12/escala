"""A content-free revision lets authenticated pages notice new application data."""
import hashlib
import json
from django.db.models import Count,Max,Q
from .models import Shift,SyncState,Exchange,Allocation,ExchangeTopic,ForumComment,SheetSource,Notification,SyncHeartbeat

def data_revision(resident=None):
    values={
        'sync':SyncState.objects.aggregate(latest=Max('last_success'),total=Count('pk')),
        'shifts':Shift.objects.aggregate(total=Count('pk'),active=Count('pk',filter=Q(active=True)),interested=Count('pk',filter=Q(wants_exchange=True)),latest=Max('pk')),
        'exchanges':Exchange.objects.aggregate(created=Max('created_at'),resolved=Max('resolved_at'),total=Count('pk')),
        'allocations':Allocation.objects.aggregate(created=Max('created_at'),resolved=Max('resolved_at'),total=Count('pk')),
        'topics':ExchangeTopic.objects.aggregate(published=Max('published_at'),closed=Max('closed_at'),total=Count('pk')),
        'comments':ForumComment.objects.aggregate(created=Max('created_at'),total=Count('pk')),
        'sources':list(SheetSource.objects.order_by('pk').values_list('pk','name','enabled','error')),
        'sync_cycle':list(SyncHeartbeat.objects.values_list('started_at','finished_at')),
    }
    if resident is not None:
        values['notifications']=Notification.objects.filter(resident=resident).aggregate(total=Count('pk'),latest=Max('created_at'),read=Max('read_at'),unread=Count('pk',filter=Q(read_at__isnull=True)))
    return hashlib.sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()

def context(request):
    # Django evaluates the callable only when the authenticated base template uses it.
    if not request.user.is_authenticated:
        return {}
    from .identity import resident_for_account
    resident=resident_for_account(request.user)
    return {
        'live_revision':lambda:data_revision(resident),
        'notifications_unread_count':lambda:Notification.objects.filter(resident=resident,read_at__isnull=True).count(),
    }
