"""A content-free revision lets authenticated pages notice new application data."""
import hashlib
import json
from django.db.models import Count,Max,Q
from .models import Shift,SyncState,Exchange,Allocation,ExchangeTopic,ForumComment,SheetSource

def data_revision():
    values={
        'sync':SyncState.objects.aggregate(latest=Max('last_success'),total=Count('pk')),
        'shifts':Shift.objects.aggregate(total=Count('pk'),active=Count('pk',filter=Q(active=True)),interested=Count('pk',filter=Q(wants_exchange=True)),latest=Max('pk')),
        'exchanges':Exchange.objects.aggregate(created=Max('created_at'),resolved=Max('resolved_at'),total=Count('pk')),
        'allocations':Allocation.objects.aggregate(created=Max('created_at'),resolved=Max('resolved_at'),total=Count('pk')),
        'topics':ExchangeTopic.objects.aggregate(published=Max('published_at'),closed=Max('closed_at'),total=Count('pk')),
        'comments':ForumComment.objects.aggregate(created=Max('created_at'),total=Count('pk')),
        'sources':list(SheetSource.objects.order_by('pk').values_list('pk','name','enabled','error')),
    }
    return hashlib.sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()

def context(request):
    # Django evaluates the callable only when the authenticated base template uses it.
    return {'live_revision':data_revision} if request.user.is_authenticated else {}
