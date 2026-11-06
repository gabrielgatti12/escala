from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import unittest


try:
    from config.access_logging import SafeAccessLogger, redact_sensitive_text
    from gunicorn.config import Config
except ModuleNotFoundError as error:
    if error.name != 'gunicorn':
        raise
    SafeAccessLogger = None


@unittest.skipUnless(SafeAccessLogger is not None, 'Gunicorn is a production-only dependency.')
class PrivateAccessLogTests(unittest.TestCase):
    def atoms(self, path, query='', *, referer=None, raw_uri=None):
        environ = {
            'REQUEST_METHOD': 'GET', 'SERVER_PROTOCOL': 'HTTP/1.1',
            'PATH_INFO': path, 'RAW_URI': raw_uri or path + (f'?{query}' if query else ''),
            'QUERY_STRING': query, 'REMOTE_ADDR': '127.0.0.1',
        }
        environ['REQUEST_URI'] = environ['RAW_URI']
        if referer:
            environ['HTTP_REFERER'] = referer
        request = SimpleNamespace(headers={'Referer': referer or '-', 'X-Original-URI': environ['RAW_URI']})
        response = SimpleNamespace(status='200 OK', sent=120, headers={'Location': environ['RAW_URI']})
        original_environ = deepcopy(environ)
        original_request = deepcopy(request.headers)
        original_response = deepcopy(response.headers)
        logger = SafeAccessLogger.__new__(SafeAccessLogger)
        atoms = logger.atoms(response, request, environ, timedelta(milliseconds=5))
        self.assertEqual(environ, original_environ)
        self.assertEqual(request.headers, original_request)
        self.assertEqual(response.headers, original_response)
        return logger, atoms

    def assert_not_logged(self, atoms, *secrets):
        rendered_atoms = '\n'.join(str(value) for value in atoms.values())
        for secret in secrets:
            self.assertNotIn(secret, rendered_atoms)

    def test_feed_path_query_and_header_atoms_are_redacted(self):
        _, atoms = self.atoms('/calendario/assinatura/fictitious-feed-token/', 'private=fictitious-query&other=fictitious-second-query')
        self.assert_not_logged(atoms, 'fictitious-feed-token', 'fictitious-query', 'fictitious-second-query')
        self.assertEqual(atoms['r'], 'GET /calendario/assinatura/[redacted]/ HTTP/1.1')
        self.assertEqual(atoms['U'], '/calendario/assinatura/[redacted]/')
        self.assertEqual(atoms['q'], '[redacted]')
        self.assertEqual(atoms['{query_string}e'], '[redacted]')

    def test_default_and_custom_formats_never_restore_private_tokens(self):
        logger, atoms = self.atoms('/calendario/assinatura/fictitious-format-token/', 'x=fictitious-format-query')
        safe_atoms = logger.atoms_wrapper_class(atoms)
        default_line = Config().access_log_format % safe_atoms
        custom_line = '%(r)s %(U)s %(q)s %({raw_uri}e)s %({path_info}e)s %({query_string}e)s %({x-original-uri}i)s' % safe_atoms
        for line in (default_line, custom_line):
            self.assertNotIn('fictitious-format-token', line)
            self.assertNotIn('fictitious-format-query', line)

    def test_activation_uid_token_and_query_are_redacted(self):
        _, atoms = self.atoms('/ativar/fictitious-uid/fictitious-activation-token/', 'next=fictitious-private-next')
        self.assert_not_logged(atoms, 'fictitious-uid', 'fictitious-activation-token', 'fictitious-private-next')
        self.assertEqual(atoms['U'], '/ativar/[redacted]/[redacted]/')

    def test_referer_private_url_is_redacted_on_an_ordinary_request(self):
        _, atoms = self.atoms('/accounts/login/', referer='https://example.test/calendario/assinatura/fictitious-referer-token/?x=fictitious-referer-query')
        self.assert_not_logged(atoms, 'fictitious-referer-token', 'fictitious-referer-query')

    def test_encoded_private_path_and_redirect_queries_are_redacted(self):
        _, atoms = self.atoms(
            '/calendario/assinatura/fictitious-encoded-token/',
            'private=fictitious-encoded-query',
            raw_uri='/%63alendario/assinatura/fictitious-encoded-token/?private=fictitious-encoded-query',
        )
        self.assert_not_logged(atoms, 'fictitious-encoded-token', 'fictitious-encoded-query')
        encoded = 'next=%2Fcalendario%2Fassinatura%2Ffictitious-redirect-token%2F'
        self.assertNotIn('fictitious-redirect-token', redact_sensitive_text(encoded))

    def test_ordinary_requests_retain_useful_access_log_fields(self):
        _, atoms = self.atoms('/accounts/login/', 'next=%2F')
        self.assertEqual(atoms['r'], 'GET /accounts/login/?next=%2F HTTP/1.1')
        self.assertEqual(atoms['U'], '/accounts/login/')
        self.assertEqual(atoms['q'], 'next=%2F')
        self.assertEqual(atoms['s'], '200')
