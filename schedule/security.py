"""Access revocation, private responses and bounded login attempts."""
from datetime import timedelta
import ipaddress
import math

from django.conf import settings
from django.contrib.auth import SESSION_KEY, logout
from django.contrib.auth.views import redirect_to_login
from django.db import DatabaseError, OperationalError, transaction
from django.db.models import F
from django.http import HttpResponse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.crypto import salted_hmac

from .identity import is_account_enabled
from .models import LoginAttempt


WINDOW = timedelta(minutes=15)
PAIR_LIMIT = 5
IP_LIMIT = 30


def client_address(request):
    # In production the host-only reverse proxy must overwrite, not append,
    # X-Forwarded-For. The same trust switch is used for HTTPS in settings.
    value = request.META.get('HTTP_X_FORWARDED_FOR') if settings.SECURE_PROXY_SSL_HEADER else None
    value = value or request.META.get('REMOTE_ADDR', '')
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return 'unknown'


def login_keys(request):
    identity = request.POST.get('username', '').strip().casefold()[:254]
    address = client_address(request)
    def digest(value):
        return salted_hmac('schedule.login-attempts', value, algorithm='sha256').hexdigest()
    return [(digest('pair|' + address + '|' + identity), PAIR_LIMIT),
            (digest('address|' + address), IP_LIMIT)]


def retry_after(keys, now):
    active = LoginAttempt.objects.filter(key__in=[key for key, _ in keys], window_started__gt=now-WINDOW)
    limits = dict(keys)
    return max([math.ceil((row.window_started+WINDOW-now).total_seconds())
                for row in active if row.failures >= limits[row.key]] or [0])


@transaction.atomic
def record_failure(keys, now):
    LoginAttempt.objects.filter(window_started__lte=now-timedelta(days=1)).delete()
    for key, _ in keys:
        row, _ = LoginAttempt.objects.get_or_create(key=key, defaults={'window_started':now})
        if row.window_started <= now-WINDOW:
            LoginAttempt.objects.filter(key=key).update(window_started=now, failures=1)
        else:
            LoginAttempt.objects.filter(key=key).update(failures=F('failures')+1)


class AccountAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        authenticated = request.user.is_authenticated
        stale_authentication = not authenticated and SESSION_KEY in request.session
        if stale_authentication:
            # Custom backends can reject a deactivated user before this guard
            # sees it. Remove the old session so reactivation cannot revive it.
            logout(request)
        if authenticated and not is_account_enabled(request.user):
            logout(request)
            response = (redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
                        if request.method in ['GET','HEAD'] else
                        HttpResponse('O acesso desta conta foi desativado.', status=403))
        else:
            response = self.get_response(request)
        if (authenticated or stale_authentication or request.user.is_authenticated) and 'no-store' not in response.get('Cache-Control',''):
            # Protect browser history/shared caches after logout, including
            # account lists and the private contact-import preview.
            add_never_cache_headers(response)
        return response

    def process_exception(self, request, exception):
        if isinstance(exception, OperationalError) and any(text in str(exception).lower() for text in ['database is locked','database table is locked']):
            response = HttpResponse('A escala está sendo atualizada. Recarregue a página e confira o resultado antes de tentar novamente.', status=409)
            response['Retry-After'] = '1'
            add_never_cache_headers(response)
            return response
        return None


class LoginThrottleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != 'POST' or request.path.rstrip('/') not in ['/accounts/login','/admin/login']:
            return self.get_response(request)
        now = timezone.now()
        keys = login_keys(request)
        try:
            remaining = retry_after(keys, now)
            if remaining:
                response = HttpResponse('Muitas tentativas de acesso. Aguarde e tente novamente.', status=429)
                response['Retry-After'] = str(remaining)
            else:
                response = self.get_response(request)
                if response.status_code == 302 and request.user.is_authenticated:
                    LoginAttempt.objects.filter(key=keys[0][0]).delete()
                elif response.status_code == 200:
                    record_failure(keys, now)
        except DatabaseError:
            # Never bypass throttling if the shared counter store is down.
            response = HttpResponse('Serviço de login indisponível. Tente novamente em instantes.', status=503)
        add_never_cache_headers(response)
        return response
