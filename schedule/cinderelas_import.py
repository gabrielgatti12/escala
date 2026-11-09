"""Persist the final monthly grid, without replaying the external Forms log."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .changes import snapshot_source, notify_schedule_changes
from .models import Exchange, ExchangeTopic, SheetSource, Shift, SyncState
from .resident_matching import resolve_roster_names
from .sheets import SheetFormatError, digest


def source_values(slot, resident):
    category=slot.get('category','cinderela')
    label={'cinderela':'Cinderela','night':'Plantão noturno','weekend':'Plantão diurno'}[category]
    if (slot['ends_at']-slot['starts_at']).total_seconds()==86400:
        label='Plantão 24h'
    return {'resident_id': resident.pk, 'starts_at': slot['starts_at'].isoformat(),
            'ends_at': slot['ends_at'].isoformat(), 'sector': f"{label} · {slot['group']}",
            'resident_group': slot['group']}


def current_values(shift):
    return {'resident_id': shift.resident_id, 'starts_at': timezone.localtime(shift.starts_at).isoformat(),
            'ends_at': timezone.localtime(shift.ends_at).isoformat(), 'sector': shift.sector,
            'resident_group': shift.resident_group}


@transaction.atomic
def sync_cinderelas(book, source_id):
    return sync_monthly_roster(book,source_id,full=False)


@transaction.atomic
def sync_plantoes(book,source_id):
    return sync_monthly_roster(book,source_id,full=True)


def sync_monthly_roster(book,source_id,*,full):
    from .cinderelas_parser import parse_cinderelas, month_key
    if full:
        from .plantao_parser import parse_plantoes
        parsed=parse_plantoes(book)
    else:
        parsed = parse_cinderelas(book)
    categories=['cinderela','night','weekend'] if full else ['cinderela']
    state, _ = SyncState.objects.get_or_create(source=source_id)
    state = SyncState.objects.select_for_update().get(pk=state.pk)
    previous_success = state.last_success
    now = timezone.now()
    before = snapshot_source(source_id, now=now) if previous_success else {}
    matches, name_issues = resolve_roster_names({slot['name'] for slot in parsed['slots']})
    warnings = list(parsed['warnings']) + name_issues
    preserved_tabs = set(parsed['preserved_tabs'])
    for slot in parsed['slots']:
        if slot['name'] not in matches:
            preserved_tabs.add(slot['tab'])
            warnings.append(f"{slot['tab']}: mês preservado por nome não associado em {slot['cell']}.")
    valid_tabs = set(parsed['recognized_tabs']) - preserved_tabs
    old_tabs = set(Shift.objects.filter(source=source_id, category__in=categories, active=True).values_list('source_tab', flat=True))
    if not valid_tabs and not old_tabs:
        raise SheetFormatError('Nenhum mês pôde ser importado com segurança. ' + ' '.join(warnings[:2]))
    recognized_months = {month_key(tab) for tab in parsed['recognized_tabs']}
    absent_tabs = {tab for tab in old_tabs if month_key(tab) not in recognized_months}
    preserved_tabs.update(absent_tabs)
    preserved_months = {month_key(tab) for tab in preserved_tabs}
    held_tabs = preserved_tabs | {tab for tab in old_tabs if month_key(tab) in preserved_months}
    valid_months = {month_key(tab) for tab in valid_tabs}
    cleanup_tabs = valid_tabs | {tab for tab in old_tabs if month_key(tab) in valid_months}
    for tab in sorted(absent_tabs):
        warnings.append(f'{tab}: aba ausente; dados anteriores preservados para conferência.')
    accepted_ids = set()
    for offered_id, requested_id in Exchange.objects.filter(status='accepted').filter(Q(offered__source=source_id) | Q(requested__source=source_id)).values_list('offered_id', 'requested_id'):
        accepted_ids.update([offered_id, requested_id])
    seen = []
    affected = set()
    protected = 0
    for slot in parsed['slots']:
        if slot['tab'] not in valid_tabs:
            continue
        resident = matches[slot['name']]
        # A renamed tab must update the same logical slot/calendar UID.
        ordinal = slot['slot_key'].rsplit('|', 1)[1]
        category=slot.get('category','cinderela')
        hours=slot.get('normalized_hours','18-24')
        suffix = f"{slot['date'].isoformat()}|{slot['group']}|{hours}|{ordinal}"
        prefix='cinderela:' if category=='cinderela' else 'plantao:'
        external_id = prefix + digest(source_id + '|' + suffix)
        seen.append(external_id)
        desired = source_values(slot, resident)
        shift = Shift.objects.select_for_update().filter(external_id=external_id).first()
        if shift is None:
            legacy_tabs = {slot['tab']} | {tab for tab in old_tabs if month_key(tab) == month_key(slot['tab'])}
            legacy_ids = [prefix + digest(source_id + '|' + tab + '|' + suffix) for tab in legacy_tabs]
            legacy = list(Shift.objects.select_for_update().filter(source=source_id, external_id__in=legacy_ids))
            if len(legacy) > 1:
                raise SheetFormatError('Há posições duplicadas para o mesmo mês; confira a fonte antes de importar.')
            if legacy:
                shift = legacy[0]
                shift.external_id = external_id
                shift.save(update_fields=['external_id'])
        if shift and shift.pk in accepted_ids:
            # An unchanged old source is expected until writeback is available.
            # A third-party edit disagreeing with both the source baseline and
            # the accepted decision requires review, never a silent overwrite.
            acknowledged = desired == current_values(shift)
            conflicting = not acknowledged and desired != shift.source_snapshot
            shift.requires_reconciliation = conflicting
            shift.source_cell = slot['cell']
            shift.source_tab = slot['tab']
            if acknowledged:
                shift.source_snapshot = desired
            shift.save(update_fields=['requires_reconciliation', 'source_cell', 'source_tab', 'source_snapshot'])
            protected += 1
            if conflicting:
                affected.add(shift.pk)
            continue
        if shift and (current_values(shift) != desired or not shift.active):
            affected.add(shift.pk)
            if shift.resident_id != resident.pk:
                shift.wants_exchange = False
                ExchangeTopic.objects.filter(shift=shift, closed_at__isnull=True).update(closed_at=now)
        fields = dict(resident=resident, starts_at=slot['starts_at'], ends_at=slot['ends_at'], sector=desired['sector'],
                      source=source_id, source_tab=slot['tab'], source_cell=slot['cell'],
                      category=category, resident_group=slot['group'], active=True,
                      source_snapshot=desired, requires_reconciliation=False)
        if shift:
            for field, value in fields.items():
                setattr(shift, field, value)
            shift.save()
        else:
            Shift.objects.create(external_id=external_id, **fields)
    obsolete = Shift.objects.filter(source=source_id, category__in=categories, source_tab__in=cleanup_tabs, active=True).exclude(external_id__in=seen)
    obsolete_protected = obsolete.filter(pk__in=accepted_ids)
    affected.update(obsolete.values_list('pk', flat=True))
    protected += obsolete_protected.count()
    obsolete_protected.update(requires_reconciliation=True)
    removed = obsolete.exclude(pk__in=accepted_ids)
    ExchangeTopic.objects.filter(shift__in=removed, closed_at__isnull=True).update(closed_at=now)
    removed.update(active=False, wants_exchange=False)
    if held_tabs:
        held = Shift.objects.filter(source=source_id, category__in=categories, source_tab__in=held_tabs, active=True)
        affected.update(held.values_list('pk', flat=True))
        held.update(requires_reconciliation=True)
    if affected:
        Exchange.objects.filter(status='pending').filter(Q(offered_id__in=affected) | Q(requested_id__in=affected)).update(status='cancelled', resolved_at=now)
    reconciliation = Shift.objects.filter(source=source_id, active=True, requires_reconciliation=True).count()
    if valid_tabs:
        state.last_success = now
    label='plantões' if full else 'cinderelas'
    state.summary = f'{len(seen)} {label} definidos; {len(valid_tabs)} meses importados; {len(preserved_tabs)} meses preservados; {protected} trocas preservadas; {reconciliation} exigem conciliação.'
    state.save()
    SheetSource.objects.filter(spreadsheet_id=source_id).update(warnings='\n'.join(dict.fromkeys(warnings))[:12000])
    if previous_success:
        notify_schedule_changes(source_id, before, snapshot_source(source_id, now=now), previous_success=previous_success)
    return state.summary
