import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from study_log import StudyLog, StudyLogError, NoRedirect, READ_SCOPES, register_study_log_tool
from tool_registry import ToolRegistry


class StudyLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(data_home=Path(self.tmp.name))
        self.service = StudyLog(self.settings)
        self.identifier = 'a' * 32

    def tearDown(self):
        self.tmp.cleanup()

    def grant(self, status='pending', **overrides):
        # Matches Study Log lib/agent-access.ts publicGrant(), including scopes.
        return {'id': self.identifier, 'status': status,
                'request_expires_at': 9999999999,
                'expires_at': 9999999999 if status == 'connected' else None,
                'scopes': sorted(READ_SCOPES), **overrides}

    def connect(self):
        with patch.object(self.service, '_request', return_value=self.grant()) as call:
            public = self.service.connect()
        private = json.loads(self.service.path.read_text())
        return public, private, call

    def test_token_remains_local_and_request_contains_only_hash(self):
        public, private, call = self.connect()
        self.assertNotIn(private['token'], json.dumps(public))
        self.assertEqual(call.call_args.args[2]['token_hash'], hashlib.sha256(private['token'].encode()).hexdigest())
        self.assertEqual(os.stat(self.service.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.service.path.parent).st_mode & 0o777, 0o700)
        self.assertIn(self.identifier, public['authorization_url'])

    def test_revoke_removes_local_credential_and_blocks_future_reads(self):
        self.connect()
        with patch.object(self.service, '_request', return_value=self.grant('revoked')):
            public = self.service.status()
        self.assertEqual(public['status'], 'revoked')
        self.assertNotIn('token', self.service.path.read_text())
        with self.assertRaises(StudyLogError):
            self.service.read('records')

    def test_read_requests_are_fixed_gets_and_pagination_is_preserved(self):
        _, private, _ = self.connect()
        with patch.object(self.service, '_request', return_value={'records': [], 'total': 250, 'next_offset': 100}) as request:
            value = self.service.read('records', limit=100, **{'from': '2026-10-01', 'to': '2026-10-07'})
        self.assertEqual(value['data']['next_offset'], 100)
        self.assertEqual(request.call_args.kwargs, {'token': private['token']})
        self.assertTrue(request.call_args.args[0].startswith('/api/agent/read/records?'))
        self.assertNotIn(private['token'], json.dumps(value))
        for resource, query in [('delete', {}), ('records', {'user_id': 'bob'}), ('projects', {'offset': 1})]:
            with self.assertRaises(StudyLogError):
                self.service.read(resource, **query)

    def test_disconnect_revokes_remote_and_network_failure_preserves_recovery(self):
        self.connect()
        with patch.object(self.service, '_request', side_effect=StudyLogError('网络失败')):
            with self.assertRaises(StudyLogError):
                self.service.disconnect()
        self.assertTrue(self.service.path.exists())
        with patch.object(self.service, '_request', return_value={'status': 'revoked'}) as request:
            self.service.disconnect()
        self.assertEqual(request.call_args.args[:2], ('/api/agent/session', 'DELETE'))
        self.assertFalse(self.service.path.exists())

    def test_read_started_before_revoke_discards_late_response(self):
        self.connect()
        reading, release = threading.Event(), threading.Event()

        def request(path, method='GET', **kwargs):
            if method == 'DELETE':
                return {'status': 'revoked'}
            reading.set()
            if not release.wait(5):
                raise TimeoutError('test did not release read')
            return {'records': [{'id': 1, 'notes': '旧授权读取的合成资料'}]}

        with patch.object(self.service, '_request', side_effect=request), ThreadPoolExecutor(1) as pool:
            result = pool.submit(self.service.read, 'records')
            try:
                self.assertTrue(reading.wait(3))
                self.assertEqual(self.service.disconnect()['status'], 'disconnected')
            finally:
                release.set()
            with self.assertRaisesRegex(StudyLogError, '授权已变更'):
                result.result(timeout=3)

    def test_failed_revoke_keeps_authorization_for_a_later_read(self):
        _, private, _ = self.connect()

        def request(path, method='GET', **kwargs):
            if method == 'DELETE':
                raise StudyLogError('网络失败', 503)
            self.assertEqual(kwargs['token'], private['token'])
            return {'records': [{'id': 1}]}

        with patch.object(self.service, '_request', side_effect=request):
            with self.assertRaisesRegex(StudyLogError, '网络失败'):
                self.service.disconnect()
            self.assertEqual(self.service.read('records')['data'], {'records': [{'id': 1}]})

    def test_redirect_and_insecure_origins_and_symlink_storage_are_rejected(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test'))
        for origin in ['http://example.com', 'https://user:secret@example.com', 'https://example.com/path', 'https://example.com#fragment']:
            with self.assertRaises(StudyLogError):
                StudyLog(self.settings, origin)
        target = Path(self.tmp.name) / 'target'
        target.mkdir()
        self.service.path.parent.symlink_to(target)
        with self.assertRaises(StudyLogError):
            self.service.status()

    def test_main_agent_tool_is_read_only_and_exposes_no_token_arguments(self):
        registry = ToolRegistry()
        register_study_log_tool(registry, lambda: self.settings)
        metadata = registry.metadata()[0]['function']
        self.assertEqual(metadata['name'], 'read_study_log')
        self.assertNotIn('token', metadata['parameters']['properties'])
        self.assertEqual(registry._tools['read_study_log'].effect, 'read')
        with self.assertRaises(StudyLogError):
            registry.call('read_study_log', {'resource': 'analytics'}, [])

    def test_invalid_or_wider_grants_never_save_credentials(self):
        for response in [self.grant(scopes=['records:write']),
                         self.grant(scopes=list(READ_SCOPES) + ['records:write']),
                         self.grant(status='connected', expires_at=None),
                         self.grant(request_expires_at=True),
                         {'id': self.identifier}]:
            with self.subTest(response=response), patch.object(self.service, '_request', return_value=response):
                with self.assertRaises(StudyLogError):
                    self.service.connect()
                self.assertFalse(self.service.path.exists())

    def test_session_must_match_pairing_identity(self):
        self.connect()
        original = self.service.path.read_text()
        with patch.object(self.service, '_request', return_value=self.grant('connected', id='b' * 32)):
            with self.assertRaises(StudyLogError):
                self.service.status()
        self.assertEqual(original, self.service.path.read_text())

    def test_approval_and_expiry_are_reflected_without_token_leak(self):
        _, private, _ = self.connect()
        with patch.object(self.service, '_request', return_value=self.grant('connected')):
            connected = self.service.status()
        self.assertEqual(connected['status'], 'connected')
        self.assertNotIn(private['token'], json.dumps(connected))
        with patch.object(self.service, '_request', return_value=self.grant('expired')):
            self.assertEqual(self.service.status()['status'], 'expired')
        self.assertNotIn('token', json.loads(self.service.path.read_text()))

    def test_unauthorized_session_discards_revoked_local_token(self):
        self.connect()
        with patch.object(self.service, '_request', side_effect=StudyLogError('授权已撤销', 401)):
            self.assertEqual(self.service.status()['status'], 'revoked')
        self.assertNotIn('token', json.loads(self.service.path.read_text()))

    def test_gateway_unauthorized_response_preserves_the_local_credential(self):
        self.connect()
        original = self.service.path.read_bytes()
        responses = (('text/html', b'<title>Login required</title>'),
                     ('application/json', b'{"error":"Unauthorized"}'))
        for operation in (self.service.status, self.service.disconnect):
            for media_type, body in responses:
                with self.subTest(operation=operation.__name__, media_type=media_type):
                    headers = Message()
                    headers['Content-Type'] = media_type
                    unauthorized = HTTPError('https://fixture.test/api/agent/session', 401, 'Unauthorized',
                                             headers, io.BytesIO(body))
                    with patch('study_log.urllib.request.build_opener') as opener:
                        opener.return_value.open.side_effect = unauthorized
                        with self.assertRaises(StudyLogError) as error:
                            operation()
                        self.assertEqual(error.exception.status, 502)
                    self.assertEqual(self.service.path.read_bytes(), original)

    def test_application_invalid_credential_response_can_clear_the_local_credential(self):
        self.connect()
        headers = Message()
        headers['Content-Type'] = 'application/json; charset=utf-8'
        body = json.dumps({'error': '授权无效，请重新连接。'}, ensure_ascii=False).encode()
        unauthorized = HTTPError('https://fixture.test/api/agent/session', 401, 'Unauthorized',
                                 headers, io.BytesIO(body))
        with patch('study_log.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = unauthorized
            self.assertEqual(self.service.status()['status'], 'revoked')
        self.assertNotIn('token', json.loads(self.service.path.read_text()))


if __name__ == '__main__':
    unittest.main()
