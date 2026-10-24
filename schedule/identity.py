from .models import ResidentAccess

def resident_for_account(account):
    if not hasattr(account, '_schedule_resident'):
        mapping = ResidentAccess.objects.select_related('resident').filter(account=account).first()
        account._schedule_resident = mapping.resident if mapping else account
    return account._schedule_resident
