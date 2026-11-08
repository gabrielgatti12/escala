"""Private, resident-scoped notifications. Reading a page never sends mail."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import DateTimeField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_POST

from .identity import resident_for_account
from .models import Notification


def resident_notifications(request):
    return Notification.objects.filter(resident=resident_for_account(request.user))


def private_response(response):
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'same-origin'
    return response


@login_required
def notification_list(request):
    notes = resident_notifications(request).annotate(
        displayed_at=Coalesce(Cast(KeyTextTransform('last_reminded_at', 'payload'), DateTimeField()), 'created_at'),
    ).order_by('-displayed_at', '-pk')
    kind = request.GET.get('kind', '')
    if kind in ['schedule', 'request', 'reminder']:
        notes = notes.filter(kind=kind)
    unread_only = request.GET.get('unread') == '1'
    if unread_only:
        notes = notes.filter(read_at__isnull=True)
    return private_response(render(request, 'notifications.html', {
        'tab': 'notifications', 'kind': kind, 'unread_only': unread_only,
        'notifications': Paginator(notes, 20).get_page(request.GET.get('page')),
        'unread_count': resident_notifications(request).filter(read_at__isnull=True).count(),
    }))


def activity_details(value):
    if not isinstance(value, dict):
        return None
    result = dict(value)
    for field in ['starts_at', 'ends_at']:
        raw = value.get(field)
        result[field] = parse_datetime(raw) if isinstance(raw, str) else None
    raw_date = value.get('date')
    result['date'] = parse_date(raw_date) if isinstance(raw_date, str) else None
    result['status_label'] = {
        'confirmed': 'Titularidade definida', 'shared_pending': 'A combinar pela dupla',
        'allocated_to_you': 'Divisão definida para você', 'allocated_to_colleague': 'Definida para o colega',
        'rest': 'Descanso após plantão', 'time_unknown': 'Horário não definido',
        'reconciliation_required': 'Decisão preservada; conferir alteração na fonte',
    }.get(value.get('status'), value.get('status', ''))
    return result


@login_required
def notification_detail(request, pk):
    note = get_object_or_404(resident_notifications(request), pk=pk)
    changes = []
    payload = note.payload if isinstance(note.payload, dict) else {}
    for item in payload.get('changes', [])[:20]:
        if isinstance(item, dict):
            changes.append({
                'label': {'added': 'Atividade adicionada', 'removed': 'Atividade removida', 'updated': 'Atividade alterada'}.get(item.get('action'), 'Alteração'),
                'before': activity_details(item.get('before')), 'after': activity_details(item.get('after')),
            })
    link = note.link if note.link.startswith('/') and not note.link.startswith('//') else ''
    return private_response(render(request, 'notification_detail.html', {
        'tab': 'notifications', 'notification': note, 'changes': changes,
        'truncated': payload.get('truncated', False), 'notification_link': link,
    }))


@login_required
@require_POST
def notification_read(request, pk):
    note = get_object_or_404(resident_notifications(request), pk=pk)
    if note.read_at is None:
        Notification.objects.filter(pk=note.pk, read_at__isnull=True).update(read_at=timezone.now())
    return redirect('notification_detail', pk=pk) if request.POST.get('detail') == '1' else redirect('notifications')


@login_required
@require_POST
def notifications_read_all(request):
    resident_notifications(request).filter(read_at__isnull=True).update(read_at=timezone.now())
    messages.success(request, 'Seus avisos foram marcados como lidos.')
    return redirect('notifications')
