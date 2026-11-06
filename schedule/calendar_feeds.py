"""Private, revocable subscriptions for a resident's confirmed schedule."""

from datetime import timezone as utc
from functools import wraps
import hashlib
import re
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_GET, require_POST, require_safe

from .identity import operational_residents, resident_for_account
from .models import CalendarSubscription, ResidentAccess, Shift


SESSION_KEY = 'calendar_subscription_link'
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{64}')


def _private_response(view=None, *, referrer_policy='same-origin'):
    def decorate(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            response = handler(*args, **kwargs)
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            response['Referrer-Policy'] = referrer_policy
            return response
        return wrapped
    return decorate(view) if view is not None else decorate


def token_digest(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _managed_resident(account):
    resident = resident_for_account(account)
    if not account.is_active or not resident.is_active:
        return None
    # A source identity with a separate login must not manage that login's link.
    mapping = ResidentAccess.objects.filter(resident=resident).first()
    if mapping and mapping.account_id != account.pk:
        return None
    return resident


def _can_connect(account, resident, subscription):
    if resident is None:
        return False
    return (
        resident.pk != account.pk
        or subscription is not None
        or operational_residents().filter(pk=resident.pk).exists()
    )


@_private_response
@login_required
@require_GET
def connections(request):
    resident = _managed_resident(request.user)
    subscription = CalendarSubscription.objects.filter(resident=resident).first() if resident else None
    can_connect = _can_connect(request.user, resident, subscription)
    subscription_enabled = bool(
        subscription and subscription.enabled and subscription.account_id == request.user.pk
    )
    feed_url = None
    stored = request.session.get(SESSION_KEY)
    if subscription_enabled and isinstance(stored, dict):
        token = stored.get('token', '')
        if (
            stored.get('resident_id') == resident.pk
            and isinstance(token, str)
            and TOKEN_PATTERN.fullmatch(token)
            and constant_time_compare(token_digest(token), subscription.token_digest)
        ):
            feed_url = request.build_absolute_uri(reverse('calendar_feed', args=[token]))
    return render(request, 'calendar_connections.html', {
        'tab': 'calendar_connections', 'resident_user': resident,
        'subscription': subscription, 'subscription_enabled': subscription_enabled,
        'feed_url': feed_url, 'can_connect': can_connect,
        'confirmed_count': Shift.objects.filter(resident=resident, active=True).count() if resident else 0,
    })


@_private_response
@login_required
@require_POST
def rotate(request):
    resident = _managed_resident(request.user)
    subscription = CalendarSubscription.objects.filter(resident=resident).first() if resident else None
    if not _can_connect(request.user, resident, subscription):
        return HttpResponseForbidden('Vincule sua conta a um residente ativo antes de gerar a assinatura.')
    token = secrets.token_urlsafe(48)
    with transaction.atomic():
        CalendarSubscription.objects.update_or_create(
            resident=resident,
            defaults={'token_digest': token_digest(token), 'enabled': True, 'account': request.user},
        )
    request.session[SESSION_KEY] = {'resident_id': resident.pk, 'token': token}
    messages.success(request, 'Link privado gerado. Links anteriores deixaram de funcionar; use o novo link no seu calendário.')
    return redirect('calendar_connections')


@_private_response
@login_required
@require_POST
def revoke(request):
    resident = _managed_resident(request.user)
    if resident is None:
        return HttpResponseForbidden('Não foi possível identificar um residente ativo para esta conta.')
    CalendarSubscription.objects.filter(resident=resident).update(enabled=False)
    request.session.pop(SESSION_KEY, None)
    messages.success(request, 'Assinatura revogada. O link deixou de permitir novas consultas. Remova também o calendário do Google se não quiser manter a cópia já recebida.')
    return redirect('calendar_connections')


def _escape_text(value):
    value = str(value).replace('\r\n', '\n').replace('\r', '\n')
    value = ''.join(char for char in value if (ord(char) >= 32 and ord(char) != 127) or char in '\n\t')
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def _fold_line(line):
    """Fold at 75 octets, including the continuation space, without splitting UTF-8."""
    lines = []
    current = ''
    size = 0
    for char in line:
        char_size = len(char.encode('utf-8'))
        if size + char_size > 75:
            lines.append(current)
            current = ' '
            size = 1
        current += char
        size += char_size
    lines.append(current)
    return '\r\n'.join(lines)


def serialize_calendar(shifts):
    """RFC 5545 text shared by downloads and subscription responses."""
    stamp = timezone.now().astimezone(utc.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Escala//Residencia//PT',
        'CALSCALE:GREGORIAN', 'X-WR-CALNAME:Minha escala',
    ]
    for shift in shifts:
        lines.extend([
            'BEGIN:VEVENT', f'UID:shift-{shift.pk}@escala', f'DTSTAMP:{stamp}',
            f'DTSTART:{shift.starts_at.astimezone(utc.utc):%Y%m%dT%H%M%SZ}',
            f'DTEND:{shift.ends_at.astimezone(utc.utc):%Y%m%dT%H%M%SZ}',
            f'SUMMARY:{_escape_text(shift.sector)}', 'END:VEVENT',
        ])
    lines.append('END:VCALENDAR')
    return '\r\n'.join(_fold_line(line) for line in lines) + '\r\n'


@_private_response(referrer_policy='no-referrer')
@require_safe
def feed(request, token):
    if not TOKEN_PATTERN.fullmatch(token):
        return HttpResponse('Assinatura indisponível.', status=404, content_type='text/plain; charset=utf-8')
    subscription = CalendarSubscription.objects.select_related('resident', 'account').filter(
        token_digest=token_digest(token), enabled=True, resident__is_active=True, account__is_active=True,
    ).first()
    if subscription is None:
        return HttpResponse('Assinatura indisponível.', status=404, content_type='text/plain; charset=utf-8')
    mapping = ResidentAccess.objects.select_related('account').filter(resident=subscription.resident).first()
    account = mapping.account if mapping else subscription.resident
    if (
        not account.is_active
        or account.pk != subscription.account_id
        or (mapping is None and not account.has_usable_password())
    ):
        return HttpResponse('Assinatura indisponível.', status=404, content_type='text/plain; charset=utf-8')
    shifts = Shift.objects.filter(resident=subscription.resident, active=True).order_by('starts_at', 'pk')
    response = HttpResponse(serialize_calendar(shifts), content_type='text/calendar; charset=utf-8')
    response['Content-Disposition'] = 'inline; filename="minha-escala.ics"'
    return response
