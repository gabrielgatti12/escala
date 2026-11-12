"""Bounded, authenticated choices loaded only when a trade dialog is opened."""
from datetime import date, datetime, time, timedelta
import re

from django.contrib.auth.decorators import login_required
from django.db.models import CharField, Q, Value
from django.db.models.functions import Concat, Lower, Replace
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from .identity import resident_for_account
from .models import Shift
from .presentation import sector_label
from .services import ON_CALL_CATEGORIES

LIMIT = 50


def _identifier(value):
    if not value:
        return None
    if not re.fullmatch(r'[0-9]{1,19}', value) or not 0 < int(value) <= 9223372036854775807:
        raise Http404
    return int(value)


def _fold(expression):
    # Keep the accent-insensitive search available in the existing browser UI.
    for accented, plain in [('áàâãÁÀÂÃ', 'a'), ('éêÉÊ', 'e'), ('íÍ', 'i'),
                            ('óôõÓÔÕ', 'o'), ('úüÚÜ', 'u'), ('çÇ', 'c')]:
        for letter in accented:
            expression = Replace(expression, Value(letter), Value(plain))
    return Lower(expression)


def _search(query, text):
    import unicodedata
    words = text.strip().split()[:8]
    for word in words:
        day = None
        try:
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', word):
                day = date.fromisoformat(word)
            elif re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', word):
                day = datetime.strptime(word, '%d/%m/%Y').date()
        except ValueError:
            return query.none()
        if day:
            if day == date.max:
                return query.none()
            start = timezone.make_aware(datetime.combine(day, time()))
            end = timezone.make_aware(datetime.combine(day + timedelta(days=1), time()))
            query = query.filter(starts_at__gte=start, starts_at__lt=end)
        elif re.fullmatch(r'\d{1,2}/\d{1,2}', word):
            day_number, month_number = map(int, word.split('/'))
            if not 1 <= day_number <= 31 or not 1 <= month_number <= 12:
                return query.none()
            query = query.filter(starts_at__day=day_number, starts_at__month=month_number)
        elif re.fullmatch(r'\d{1,2}:\d{2}', word):
            hour, minute = map(int, word.split(':'))
            query = query.filter(starts_at__hour=hour, starts_at__minute=minute)
        else:
            word = ''.join(c for c in unicodedata.normalize('NFKD', word) if not unicodedata.combining(c)).casefold()
            query = query.alias(search_text=_fold(Concat(
                'sector', Value(' '), 'resident__first_name', Value(' '),
                'resident__last_name', Value(' '), 'resident__username', output_field=CharField(),
            ))).filter(search_text__contains=word)
    return query


def _option(shift, include_resident):
    start, end = timezone.localtime(shift.starts_at), timezone.localtime(shift.ends_at)
    label = f'{start:%d/%m/%Y} · {start:%H:%M}–{end:%H:%M} · {sector_label(shift.sector)}'
    if shift.category != 'regular':
        label += f' · {shift.get_category_display()} {shift.resident_group}'
    if include_resident:
        label = f'{shift.resident.get_full_name() or shift.resident.username} · {label}'
    return {'id': str(shift.pk), 'label': label, 'category': shift.category, 'group': shift.resident_group}


@login_required
@require_GET
def choices(request):
    side = request.GET.get('side', '')
    if side not in ['offered', 'requested']:
        return JsonResponse({'error': 'Selecione o lado da troca.'}, status=400)
    person = resident_for_account(request.user)
    eligible = Shift.objects.filter(
        active=True, resident__is_active=True, starts_at__gt=timezone.now(), requires_reconciliation=False,
    ).filter(Q(category='regular') | Q(category__in=ON_CALL_CATEGORIES, resident_group__in=['R1', 'R2', 'R3']))
    own = side == 'offered'
    query = eligible.filter(resident=person) if own else eligible.exclude(resident=person)
    anchor_id = _identifier(request.GET.get('anchor', ''))
    selected_id = _identifier(request.GET.get('selected', ''))
    if anchor_id:
        opposite = eligible.exclude(resident=person) if own else eligible.filter(resident=person)
        anchor = opposite.filter(pk=anchor_id).first()
        if anchor is None:
            raise Http404
        query = query.filter(category=anchor.category)
        if anchor.category in ON_CALL_CATEGORIES:
            groups = ['R1'] if anchor.resident_group == 'R1' else ['R2', 'R3']
            query = query.filter(resident_group__in=groups)
    selected = query.select_related('resident').filter(pk=selected_id).first() if selected_id else None
    searched = _search(query, request.GET.get('q', '')[:150]).select_related('resident').order_by('starts_at', 'pk')
    rows = list(searched[:LIMIT + 1])
    has_more = len(rows) > LIMIT
    rows = rows[:LIMIT]
    if selected and not any(row.pk == selected.pk for row in rows):
        if len(rows) == LIMIT:
            rows.pop()
            has_more = True
        rows.insert(0, selected)
    response = JsonResponse({'options': [_option(row, not own) for row in rows], 'has_more': has_more,
                             'selected': str(selected.pk) if selected else '', 'limit': LIMIT})
    response['Cache-Control'] = 'private, no-store'
    return response
