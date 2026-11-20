"""Reconcile a manual undo using a fresh authenticated Google read, never XLSX cache.

An accepted local decision remains protected until a previously confirmed Google
write can be checked in full. Names, dates, hours, sectors and local ownership
must still identify that same decision. A partial edit is a conflict, not an undo.
"""
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .google_editor import EditorError, authorized_session
from .identity import is_account_enabled
from .models import (
    BundleExchange, BundleExchangeItem, Exchange, ExchangeTopic, Notification,
    SheetsEditorConnection, SheetsSourceCheck, SheetsWritePreview, Shift,
)
from .sheets_write import TAB, _bundle_snapshot, _decision, _entries, _is_bundle, _matches, _read_book, _slot


@dataclass
class SourceCheckResult:
    checked: int = 0
    reverted: int = 0
    conflicts: int = 0
    unavailable: int = 0


def _target(preview):
    return {'bundle_id': preview.bundle_id} if preview.bundle_id else {'exchange_id': preview.exchange_id}


def _same_metadata(shift, record):
    if not isinstance(record, dict) or record.get('id') != shift.pk:
        return False
    if any(record.get(key) != getattr(shift, field) for key, field in [
        ('source', 'source'), ('tab', 'source_tab'), ('cell', 'source_cell'), ('sector', 'sector'),
    ]):
        return False
    try:
        return parse_datetime(record['start']) == shift.starts_at and parse_datetime(record['end']) == shift.ends_at
    except (KeyError, TypeError, ValueError):
        return False


def _validate_local(preview, decision, entries, *, reverted=False):
    """Compare only the historical plan's stable fields, including old single plans."""
    plan = preview.plan
    if (preview.state != ('reverted' if reverted else 'done')
            or decision.status != ('reverted' if reverted else 'accepted') or not decision.resolved_at
            or not isinstance(plan, dict) or plan.get('exchange') != decision.pk
            or plan.get('decision_type', 'single') != ('bundle' if _is_bundle(decision) else 'single')
            or plan.get('spreadsheet_id') != preview.source.spreadsheet_id
            or preview.source.format != 'weekly' or not preview.source.enabled):
        raise EditorError('A troca ou sua fonte mudou desde o envio confirmado. Confira a divergência.')
    rows = plan.get('app_state')
    if not isinstance(rows, list) or len(rows) != len(entries):
        raise EditorError('O envio não contém os dados necessários para conferir todas as atividades.')
    records = {record.get('id'): record for record in rows if isinstance(record, dict)}
    if len(records) != len(entries):
        raise EditorError('O envio contém atividades inválidas ou repetidas.')
    for shift, old_owner, new_owner, side in entries:
        record = records.get(shift.pk)
        if (not _same_metadata(shift, record) or not shift.active or shift.category != 'regular'
                or shift.source_tab != TAB or shift.source != preview.source.spreadsheet_id
                or shift.resident_id != (old_owner.pk if reverted else new_owner.pk) or record.get('owner') != new_owner.pk
                or ('original_owner' in record and record['original_owner'] != old_owner.pk)
                or ('side' in record and record['side'] != side)):
            raise EditorError('A atividade ou sua titularidade mudou depois do envio. Nenhum nome foi restaurado.')
    ids = [entry[0].pk for entry in entries]
    singles = Exchange.objects.filter(status='accepted').filter(Q(offered_id__in=ids) | Q(requested_id__in=ids))
    bundles = BundleExchangeItem.objects.filter(exchange__status='accepted', shift_id__in=ids)
    if _is_bundle(decision):
        _bundle_snapshot(decision, entries)
        if plan.get('bundle_fingerprint') != decision.fingerprint:
            raise EditorError('Os termos do conjunto mudaram desde o envio confirmado.')
        bundles = bundles.exclude(exchange=decision)
    else:
        singles = singles.exclude(pk=decision.pk)
    if singles.exists() or bundles.exists():
        raise EditorError('Há outra troca aceita envolvendo estas atividades. A conciliação precisa ser manual.')
    if SheetsWritePreview.objects.filter(**_target(preview), state='writing').exists():
        raise EditorError('Há um envio desta troca em andamento. Aguarde para conferir a origem.')


def _source_state(preview, decision, entries, sheet, cells, *, reverted=False):
    _validate_local(preview, decision, entries, reverted=reverted)
    if preview.plan.get('sheet_id') != sheet.get('properties', {}).get('sheetId'):
        raise EditorError('A aba de origem foi substituída. Confira a divergência antes de restaurar nomes.')
    slots = [_slot(sheet, cells, shift, old_owner, new_owner) for shift, old_owner, new_owner, _ in entries]
    for slot, (_, old_owner, new_owner, _) in zip(slots, entries):
        if any(_matches(cell['name'], old_owner) and _matches(cell['name'], new_owner) for cell in slot['cells']):
            raise EditorError('Um nome na planilha identifica os dois residentes. Confira os cadastros antes de conciliar.')
    try:
        expected = [(cell['row'], cell['column']) for cell in preview.plan['cells']]
        actual = [(cell['row'], cell['column']) for slot in slots for cell in slot['cells']]
        valid = bool(expected) and len(expected) == len(set(expected)) and sorted(expected) == sorted(actual)
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise EditorError('As células ou a quantidade de nomes mudaram desde o envio. Confira a origem.')
    states = {slot['state'] for slot in slots}
    if len(states) != 1:
        raise EditorError('A planilha desfez apenas parte da troca. As titularidades foram preservadas para conciliação.')
    # No tokens, formulas or personal names in the digest/audit payload.
    proof = {'sheet': preview.plan['sheet_id'], 'states': sorted(states), 'cells': sorted(actual),
             'metadata': preview.plan['app_state']}
    digest = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return states.pop(), digest


def _audit(preview, account, status, message, *, restored=(), digest=''):
    return SheetsSourceCheck.objects.create(
        preview=preview, account=account, status=status, message=message[:250],
        restored_shift_ids=list(restored), source_digest=digest,
    )


def _conflict(preview, account, message):
    """Do not reinterpret partial changes or cancel other decisions."""
    with transaction.atomic():
        current = SheetsWritePreview.objects.select_for_update().get(pk=preview.pk)
        decision = _decision(current)
        if current.state != 'done' or decision.status != 'accepted':
            return False
        ids = list(decision.items.values_list('shift_id', flat=True)) if _is_bundle(decision) else [decision.offered_id, decision.requested_id]
        Shift.objects.filter(pk__in=ids).update(requires_reconciliation=True)
        _audit(current, account, 'conflict', message)
        return True


@transaction.atomic
def _confirm_current(preview, account, digest):
    current = SheetsWritePreview.objects.select_for_update().select_related('source').get(pk=preview.pk)
    decision = _decision(current)
    entries = _entries(decision)
    locked = {shift.pk: shift for shift in Shift.objects.select_for_update().filter(
        pk__in=[entry[0].pk for entry in entries]).order_by('pk')}
    entries = [(locked[shift.pk], old_owner, new_owner, side) for shift, old_owner, new_owner, side in entries]
    _validate_local(current, decision, entries)
    target = {'preview__' + field: value for field, value in _target(current).items()}
    previous = SheetsSourceCheck.objects.filter(**target, status__in=['conflict', 'current', 'reverted']).first()
    if previous and previous.status == 'conflict':
        # All slots and local terms just matched; only this authenticated check
        # can resolve a source-check conflict, not a possibly stale XLSX export.
        Shift.objects.filter(pk__in=locked).update(requires_reconciliation=False)
    _audit(current, account, 'current', 'A leitura autenticada confirmou os nomes da troca na planilha.', digest=digest)


@transaction.atomic
def _check_reverted_baseline(preview, account, *, conflict, digest=''):
    """An edit before export acknowledgement needs review, never a new auto-swap."""
    current = SheetsWritePreview.objects.select_for_update().select_related('source').get(pk=preview.pk)
    decision = _decision(current)
    entries = _entries(decision)
    locked = {shift.pk: shift for shift in Shift.objects.select_for_update().filter(
        pk__in=[entry[0].pk for entry in entries]).order_by('pk')}
    entries = [(locked[shift.pk], old_owner, new_owner, side) for shift, old_owner, new_owner, side in entries]
    try:
        _validate_local(current, decision, entries, reverted=True)
    except EditorError:
        # A subsequent accepted decision or changed local activity takes precedence.
        return False
    if not any(isinstance(shift.source_snapshot, dict) and shift.source_snapshot.get('google_revert_export_pending') for shift in locked.values()):
        return False
    for shift, _, new_owner, _ in entries:
        snapshot = dict(shift.source_snapshot) if isinstance(shift.source_snapshot, dict) else {}
        if conflict:
            # The export may already have acknowledged only part of a bundle.
            # Re-arm the guard for every slot until this new divergence is read
            # authoritatively as resolved, keeping the whole decision together.
            snapshot.update(google_revert_check_conflict=True, google_revert_export_pending=True,
                            google_revert_previous_owner=new_owner.pk)
            shift.requires_reconciliation = True
        elif snapshot.pop('google_revert_check_conflict', False):
            shift.requires_reconciliation = False
        shift.source_snapshot = snapshot
        shift.save(update_fields=['source_snapshot', 'requires_reconciliation'])
    message = ('A planilha mudou novamente antes de confirmar a reversão no export. Os nomes locais foram preservados para conciliação.'
               if conflict else 'A leitura autenticada confirmou os residentes originais; aguardando o export reconhecer a reversão.')
    _audit(current, account, 'conflict' if conflict else 'current', message, digest=digest)
    return conflict


@transaction.atomic
def _restore(preview, account, digest):
    """Restore the whole decision under row locks, keeping event ids and consent history."""
    current = SheetsWritePreview.objects.select_for_update().select_related('source').get(pk=preview.pk)
    model = BundleExchange if current.bundle_id else Exchange
    key = current.bundle_id or current.exchange_id
    model.objects.select_for_update().get(pk=key)
    decision = _decision(current)
    entries = _entries(decision)
    locked = {shift.pk: shift for shift in Shift.objects.select_for_update().filter(
        pk__in=[entry[0].pk for entry in entries]).order_by('pk')}
    entries = [(locked[shift.pk], old_owner, new_owner, side) for shift, old_owner, new_owner, side in entries]
    _validate_local(current, decision, entries)
    now = timezone.now()
    for shift, old_owner, new_owner, _ in entries:
        snapshot = dict(shift.source_snapshot) if isinstance(shift.source_snapshot, dict) else {}
        snapshot.update(google_revert_export_pending=True, google_revert_previous_owner=new_owner.pk)
        shift.resident_id = old_owner.pk
        shift.wants_exchange = False
        shift.requires_reconciliation = False
        shift.source_snapshot = snapshot
        shift.save(update_fields=['resident', 'wants_exchange', 'requires_reconciliation', 'source_snapshot'])
    decision.status = 'reverted'
    decision.source_reverted_at = now
    decision.save(update_fields=['status', 'source_reverted_at'])
    message = 'A leitura autenticada confirmou a reversão completa na planilha; nomes originais restaurados no aplicativo.'
    SheetsWritePreview.objects.filter(**_target(current), state='done').update(state='reverted', message=message)
    ids = sorted(locked)
    audit = _audit(current, account, 'reverted', message, restored=ids, digest=digest)
    # Only pending requests made against the now-restored ownership are invalidated.
    from .bundle_integrations import cancel_pending_bundles
    from .reminders import close_request_notifications
    pending = Exchange.objects.select_for_update().filter(status='pending').filter(
        Q(offered_id__in=ids) | Q(requested_id__in=ids))
    for proposal in pending:
        proposal.status = 'cancelled'
        proposal.resolved_at = now
        proposal.save(update_fields=['status', 'resolved_at'])
        close_request_notifications(proposal, now=now)
    cancel_pending_bundles(ids, now=now)
    ExchangeTopic.objects.filter(shift_id__in=ids, closed_at__isnull=True).update(closed_at=now)
    close_request_notifications(decision, now=now)
    kind = 'bundle' if _is_bundle(decision) else 'exchange'
    for resident_id in {decision.proposer_id, decision.recipient_id}:
        Notification.objects.get_or_create(
            dedup_key=f'sheet-revert:{kind}:{decision.pk}:{resident_id}',
            defaults={'resident_id': resident_id, 'kind': 'schedule', 'title': 'Troca desfeita na planilha',
                      'message': 'A troca foi desfeita na planilha original. A leitura autenticada confirmou a alteração e o calendário voltou aos residentes originais.',
                      'payload': {'request_type': kind, 'request_id': decision.pk, 'status': 'reverted',
                                  'source_check_id': audit.pk, 'shift_ids': ids},
                      'link': '/?scope=mine'},
        )


def reconcile_confirmed_writes(account):
    """Run only for an admin POST; perform authenticated GETs, never remote writes."""
    result = SourceCheckResult()
    if not account.is_superuser or not is_account_enabled(account):
        return result
    pending = Shift.objects.filter(source_snapshot__google_revert_export_pending=True).values('pk')
    accepted = Q(state='done') & (Q(exchange__status='accepted') | Q(bundle__status='accepted'))
    reverted = Q(state='reverted') & (Q(exchange__status='reverted') | Q(bundle__status='reverted')) & (
        Q(exchange__offered_id__in=pending) | Q(exchange__requested_id__in=pending) | Q(bundle__items__shift_id__in=pending))
    rows = SheetsWritePreview.objects.filter(accepted | reverted).select_related('source').distinct().order_by('-completed_at', '-pk')
    # The connection belongs to one login; other admins must not borrow its token.
    connection = SheetsEditorConnection.objects.filter(key='pilot', connected_by=account).first()
    if not connection:
        result.unavailable = rows.count()
        return result
    grouped = defaultdict(list)
    seen = set()
    for preview in rows.filter(account=account):
        key = ('bundle', preview.bundle_id) if preview.bundle_id else ('single', preview.exchange_id)
        if key in seen:
            continue
        seen.add(key)
        grouped[preview.source_id].append(preview)
    if not grouped:
        return result
    try:
        with authorized_session(account) as session:
            for previews in grouped.values():
                source = previews[0].source
                try:
                    sheet, cells = _read_book(session, source)
                except Exception:
                    # OAuth/transport errors can include secrets: never echo their text.
                    for preview in previews:
                        _audit(preview, account, 'unavailable', 'Não foi possível conferir a fonte Google; titularidades locais preservadas.')
                    result.unavailable += len(previews)
                    continue
                for preview in previews:
                    try:
                        decision = _decision(preview)
                        entries = _entries(decision)
                        already_reverted = preview.state == 'reverted'
                        state, digest = _source_state(preview, decision, entries, sheet, cells, reverted=already_reverted)
                        if already_reverted:
                            result.conflicts += int(_check_reverted_baseline(preview, account, conflict=state != 'old', digest=digest))
                        elif state == 'old':
                            _restore(preview, account, digest)
                            result.reverted += 1
                        else:
                            _confirm_current(preview, account, digest)
                        result.checked += 1
                    except EditorError as error:
                        if preview.state == 'reverted':
                            result.conflicts += int(_check_reverted_baseline(preview, account, conflict=True))
                        else:
                            result.conflicts += int(_conflict(preview, account, str(error)))
    except EditorError:
        for previews in grouped.values():
            for preview in previews:
                _audit(preview, account, 'unavailable', 'Reconecte a conta Google para conferir a reversão; titularidades locais preservadas.')
        result.unavailable += sum(map(len, grouped.values()))
    return result


def source_conflict_shift_ids(source):
    """Keep unresolved authenticated-read conflicts visible across cached imports."""
    checks = SheetsSourceCheck.objects.filter(
        preview__source__spreadsheet_id=source, status__in=['conflict', 'current', 'reverted'],
    ).filter(Q(preview__exchange__status='accepted') | Q(preview__bundle__status='accepted')).select_related(
        'preview__exchange', 'preview__bundle',
    ).order_by('-verified_at', '-pk')
    seen, result = set(), set()
    for check in checks:
        preview = check.preview
        key = ('bundle', preview.bundle_id) if preview.bundle_id else ('single', preview.exchange_id)
        if key in seen:
            continue
        seen.add(key)
        if check.status != 'conflict':
            continue
        if preview.bundle_id:
            ids = BundleExchangeItem.objects.filter(exchange_id=preview.bundle_id, shift__source=source).values_list('shift_id', flat=True)
            result.update(ids)
        else:
            result.update(Shift.objects.filter(pk__in=[preview.exchange.offered_id, preview.exchange.requested_id], source=source).values_list('pk', flat=True))
    return result


def reverted_identity_slots(source):
    """Identity-only import hints; the XLSX importer handles export acknowledgement.

    These rows never re-enable accepted-decision protection. A newer accepted
    exchange takes precedence; changed metadata/owners invalidate the hint.
    """
    checks = SheetsSourceCheck.objects.filter(status='reverted', preview__source__spreadsheet_id=source).filter(
        Q(preview__exchange__status='reverted') | Q(preview__bundle__status='reverted'),
    ).select_related('preview__exchange', 'preview__bundle').order_by('-verified_at', '-pk')
    result = {}
    for check in checks:
        preview = check.preview
        decision = _decision(preview)
        try:
            entries = _entries(decision)
        except EditorError:
            continue
        plan = preview.plan if isinstance(preview.plan, dict) else {}
        rows = plan.get('app_state', [])
        if not isinstance(rows, list):
            continue
        records = {row.get('id'): row for row in rows if isinstance(row, dict)}
        ids = [entry[0].pk for entry in entries]
        accepted = set(Exchange.objects.filter(status='accepted').filter(
            Q(offered_id__in=ids) | Q(requested_id__in=ids)).values_list('offered_id', 'requested_id'))
        protected = {pk for pair in accepted for pk in pair}
        protected.update(BundleExchangeItem.objects.filter(exchange__status='accepted', shift_id__in=ids).values_list('shift_id', flat=True))
        for shift, old_owner, _, _ in entries:
            if (shift.pk not in result and shift.pk not in protected and shift.pk in check.restored_shift_ids
                    and shift.active and shift.source == source and shift.resident_id == old_owner.pk
                    and _same_metadata(shift, records.get(shift.pk))):
                result[shift.pk] = shift
    return list(result.values())
