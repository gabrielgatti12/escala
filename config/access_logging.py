"""Keep private calendar and account activation tokens out of access logs."""
import re
from urllib.parse import unquote, urlsplit

from gunicorn.glogging import Logger


REDACTED = '[redacted]'
_FEED_PATH = re.compile(r'(/calendario/assinatura/)[^/?#\s]+(/?)(\?[^\s#]*)?')
_ACTIVATION_PATH = re.compile(r'(/ativar/)[^/?#\s]+/[^/?#\s]+(/?)(\?[^\s#]*)?')


def _encoded_path_pattern(path):
    # Each character may arrive literally or percent-encoded. Matching only the
    # sensitive URL avoids decoding unrelated %0A sequences into log newlines.
    return ''.join(f'(?:{re.escape(char)}|%{ord(char):02x})' for char in path)


_ENCODED_FEED = re.compile(_encoded_path_pattern('/calendario/assinatura/') + r'[^\s#]+', re.I)
_ENCODED_ACTIVATION = re.compile(_encoded_path_pattern('/ativar/') + r'[^\s#]+', re.I)


def redact_sensitive_text(value):
    """Redact private paths wherever a log atom contains them, including headers."""
    if not isinstance(value, str):
        return value
    value = _ENCODED_FEED.sub(f'/calendario/assinatura/{REDACTED}/', value)
    value = _ENCODED_ACTIVATION.sub(f'/ativar/{REDACTED}/{REDACTED}/', value)
    value = _FEED_PATH.sub(
        lambda match: f'{match[1]}{REDACTED}{match[2]}'
        + (f'?{REDACTED}' if match[3] else ''), value,
    )
    return _ACTIVATION_PATH.sub(
        lambda match: f'{match[1]}{REDACTED}/{REDACTED}{match[2]}'
        + (f'?{REDACTED}' if match[3] else ''), value,
    )


class SafeAccessLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        # Work on a copy: routing, private feed authentication and application
        # error handling must still receive the actual request unchanged.
        safe_environ = {key: redact_sensitive_text(value) for key, value in environ.items()}
        path = unquote(environ.get('PATH_INFO') or urlsplit(environ.get('RAW_URI', '')).path)
        safe_path = None
        if path.startswith('/calendario/assinatura/'):
            safe_path = f'/calendario/assinatura/{REDACTED}/'
        elif path.startswith('/ativar/'):
            safe_path = f'/ativar/{REDACTED}/{REDACTED}/'
        if safe_path:
            has_query = bool(environ.get('QUERY_STRING')) or '?' in environ.get('RAW_URI', '')
            safe_uri = safe_path + (f'?{REDACTED}' if has_query else '')
            safe_environ['PATH_INFO'] = safe_path
            safe_environ['RAW_URI'] = safe_uri
            if 'REQUEST_URI' in safe_environ:
                safe_environ['REQUEST_URI'] = safe_uri
            safe_environ['QUERY_STRING'] = REDACTED if has_query else ''
        atoms = super().atoms(resp, req, safe_environ, request_time)
        # Gunicorn also exposes request/response headers as custom atoms. A
        # Referer or Location containing a private URL needs the same treatment.
        return {key: redact_sensitive_text(value) for key, value in atoms.items()}
