from .models import ResidentAccess

def is_account_enabled(account):
    """Require an active login and resident, and keep source identities read-only."""
    if not account.is_active:
        return False
    resident_active = ResidentAccess.objects.filter(account=account).values_list('resident__is_active', flat=True).first()
    if resident_active is False:
        return False
    return not ResidentAccess.objects.filter(resident=account).exclude(account=account).exists()

def resident_for_account(account):
    if not hasattr(account, '_schedule_resident'):
        mapping = ResidentAccess.objects.select_related('resident').filter(account=account).first()
        account._schedule_resident = mapping.resident if mapping else account
    return account._schedule_resident

def operational_residents():
    from django.contrib.auth import get_user_model
    from django.db.models import Exists,OuterRef,Q
    from .models import Shift,SharedActivity,IncompleteActivity
    return get_user_model().objects.annotate(
        has_shift=Exists(Shift.objects.filter(resident_id=OuterRef('pk'),active=True)),
        has_shared=Exists(SharedActivity.objects.filter(residents=OuterRef('pk'),active=True)),
        has_incomplete=Exists(IncompleteActivity.objects.filter(residents=OuterRef('pk'))),
    ).filter(Q(has_shift=True)|Q(has_shared=True)|Q(has_incomplete=True))
