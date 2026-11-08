"""Record personal, effective calendar changes from a successful Sheets import.

Snapshots deliberately ignore technical row IDs and spreadsheet row positions:
moving a row or importing the same content again does not change the calendar.
Only the resident's own entries are copied to notification payloads.
"""
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json

from django.db import transaction
from django.utils import timezone

from .models import IncompleteActivity, SharedActivity, Shift


MAX_CHANGE_ITEMS = 20


def _timed_record(activity, kind, status):
    start = timezone.localtime(activity.starts_at)
    end = timezone.localtime(activity.ends_at)
    return {
        'kind': kind,
        'date': start.date().isoformat(),
        'starts_at': start.isoformat(),
        'ends_at': end.isoformat(),
        'sector': activity.sector,
        'status': status,
        '_cell': activity.source_cell,
        '_tab': activity.source_tab,
    }


def snapshot_source(source, *, now=None):
    """Return future/current entries by resident identity, never by login alias.

    Completed duties and historical incomplete records are excluded. Passing the
    same ``now`` before and after import prevents a clock boundary creating a
    false removal while the workbook is being processed.
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    result = defaultdict(list)
    for shift in Shift.objects.filter(source=source, active=True, ends_at__gt=now):
        result[shift.resident_id].append(_timed_record(shift, 'shift', 'reconciliation_required' if shift.requires_reconciliation else 'confirmed'))
    for activity in SharedActivity.objects.filter(
        source=source, active=True, ends_at__gt=now,
    ).prefetch_related('residents'):
        participants = sorted(person.pk for person in activity.residents.all())
        for resident_id in participants:
            status = 'shared_pending'
            if activity.allocated_to_id:
                status = ('allocated_to_you' if activity.allocated_to_id == resident_id
                          else 'allocated_to_colleague')
            record = _timed_record(activity, 'shared', status)
            # Membership participates in comparison, but IDs of colleagues are
            # not included in the public before/after details.
            record['_participants'] = participants
            result[resident_id].append(record)
    for activity in IncompleteActivity.objects.filter(
        source=source, date__gte=today,
    ).prefetch_related('residents'):
        participants = sorted(person.pk for person in activity.residents.all())
        for resident_id in participants:
            result[resident_id].append({
                'kind': 'incomplete',
                'date': activity.date.isoformat(),
                'sector': activity.sector,
                'raw_time': activity.raw_time,
                'status': 'rest' if activity.is_rest else 'time_unknown',
                '_cell': activity.source_cell,
                '_tab': activity.source_tab,
                '_participants': participants,
            })
    return dict(result)


def _semantic_key(record):
    # Cell position helps pair an edit for presentation, but moving an otherwise
    # equal entry must not notify its resident.
    return json.dumps(
        {key: value for key, value in record.items() if key not in ['_cell','_tab']},
        sort_keys=True, ensure_ascii=False,
    )


def _public_record(record):
    if record is None:
        return None
    return {key: value for key, value in record.items() if not key.startswith('_')}


def _change(action, before=None, after=None):
    item = {'action': action, 'before': _public_record(before), 'after': _public_record(after)}
    if before and after and before.get('_participants') != after.get('_participants'):
        item['participants_changed'] = True
    return item


def _changes(before, after):
    old = defaultdict(list)
    new = defaultdict(list)
    for record in before:
        old[_semantic_key(record)].append(record)
    for record in after:
        new[_semantic_key(record)].append(record)
    # Multisets retain actual duplicates while cancelling all unchanged duties.
    for key in old.keys() & new.keys():
        common = min(len(old[key]), len(new[key]))
        old[key] = old[key][common:]
        new[key] = new[key][common:]
    removed = [record for records in old.values() for record in records]
    added = [record for records in new.values() for record in records]
    changes = []
    for record in removed:
        match = next((candidate for candidate in added
                      if record.get('_cell') and candidate.get('_cell') == record['_cell'] and candidate.get('_tab') == record.get('_tab')), None)
        if match is None:
            changes.append(_change('removed', before=record))
        else:
            added.remove(match)
            changes.append(_change('updated', before=record, after=match))
    changes.extend(_change('added', after=record) for record in added)
    return sorted(changes, key=lambda item: (
        (item['after'] or item['before'])['date'],
        (item['after'] or item['before']).get('starts_at', ''),
        (item['after'] or item['before'])['sector'],
        item['action'],
    ))


def _description(record):
    if not record:
        return ''
    # Snapshot timestamps already use the configured local time zone.
    year, month, day = record['date'].split('-')
    date_label = f'{day}/{month}/{year}'
    if 'starts_at' in record:
        start = record['starts_at'][11:16]
        end = record['ends_at'][11:16]
        time_label = f'{start}–{end}'
    else:
        time_label = record.get('raw_time') or 'horário a confirmar'
    allocation_labels = {
        'shared_pending': 'dois nomes na origem; definição pendente',
        'allocated_to_you': 'atividade definida para você',
        'allocated_to_colleague': 'atividade definida para o colega',
        'reconciliation_required': 'decisão preservada; alteração na fonte exige conferência',
    }
    status_label = allocation_labels.get(record['status'], '')
    suffix = f' ({status_label})' if status_label else ''
    return f'{date_label} {time_label} · {record["sector"]}{suffix}'


def _message(changes):
    count = len(changes)
    lines = [f'A planilha alterou {count} atividade{"s" if count != 1 else ""} da sua escala.']
    for item in changes[:3]:
        if item['action'] == 'updated':
            line = f'Antes: {_description(item["before"])}. Agora: {_description(item["after"])}.'
            if item.get('participants_changed'):
                line += ' Os nomes indicados nessa atividade foram alterados.'
        elif item['action'] == 'added':
            line = f'Adicionada: {_description(item["after"])}.'
        else:
            line = f'Removida: {_description(item["before"])}.'
        lines.append(line)
    if count > 3:
        lines.append(f'Confira as outras {count - 3} alterações na sua escala.')
    return '\n'.join(lines)


@transaction.atomic
def notify_schedule_changes(source, before, after, *, previous_success):
    """Persist one summary per affected resident, inside the import transaction.

    An initial successful import only establishes the baseline. Its historical
    data and unchanged subsequent imports never produce personal notifications.
    """
    if previous_success is None:
        return []
    from .models import Notification
    notifications = []
    for resident_id in sorted(before.keys() | after.keys()):
        changes = _changes(before.get(resident_id, []), after.get(resident_id, []))
        if not changes:
            continue
        counts = {action: sum(item['action'] == action for item in changes)
                  for action in ('added', 'removed', 'updated')}
        counts['total'] = len(changes)
        payload = {
            'source': source,
            'counts': counts,
            'changes': changes[:MAX_CHANGE_ITEMS],
            'truncated': len(changes) > MAX_CHANGE_ITEMS,
        }
        event_key = json.dumps({
            'source': source, 'resident': resident_id,
            'previous_success': previous_success.isoformat(), 'changes': changes,
        }, sort_keys=True, ensure_ascii=False)
        dedup_key = 'schedule:' + hashlib.sha256(event_key.encode()).hexdigest()
        date_value = (changes[0]['after'] or changes[0]['before'])['date']
        changed_day = date.fromisoformat(date_value)
        week = changed_day - timedelta(days=changed_day.weekday())
        notification, created = Notification.objects.get_or_create(
            dedup_key=dedup_key,
            defaults={
                'resident_id': resident_id, 'kind': 'schedule',
                'title': 'Há uma divergência na sua escala' if any((item['after'] or {}).get('status') == 'reconciliation_required' for item in changes) else 'Sua escala foi atualizada', 'message': _message(changes),
                'payload': payload, 'link': f'/?week={week.isoformat()}&scope=mine',
            },
        )
        if created:
            notifications.append(notification)
    return notifications
