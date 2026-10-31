import io
import json
import urllib.error
from unittest import mock

from django.core.mail import send_mail
from django.test import SimpleTestCase, override_settings


@override_settings(EMAIL_BACKEND='api.brevo_backend.BrevoEmailBackend', BREVO_API_KEY='test-key',
                   DEFAULT_FROM_EMAIL='Recruit AI <sender@example.com>')
class BrevoBackendTests(SimpleTestCase):
    def fake_response(self, status=201):
        resp = mock.MagicMock()
        resp.status = status
        resp.__enter__.return_value = resp
        return resp

    def test_posts_the_message_to_brevo(self):
        with mock.patch('urllib.request.urlopen', return_value=self.fake_response()) as up:
            sent = send_mail('Your code', 'Code 123456', None, ['a@example.com'])
        self.assertEqual(sent, 1)
        req = up.call_args[0][0]
        self.assertEqual(req.full_url, 'https://api.brevo.com/v3/smtp/email')
        self.assertEqual(req.get_header('Api-key'), 'test-key')
        body = json.loads(req.data)
        self.assertEqual(body['sender'], {'email': 'sender@example.com', 'name': 'Recruit AI'})
        self.assertEqual(body['to'], [{'email': 'a@example.com'}])
        self.assertEqual(body['textContent'], 'Code 123456')

    def test_http_error_raises(self):
        with mock.patch('urllib.request.urlopen', side_effect=OSError('down')):
            with self.assertRaises(OSError):
                send_mail('x', 'y', None, ['a@example.com'])

    @override_settings(BREVO_API_KEY='')
    def test_missing_key_raises(self):
        with self.assertRaises(ValueError):
            send_mail('x', 'y', None, ['a@example.com'])

    def test_brevo_error_message_is_shown(self):
        error = urllib.error.HTTPError('https://api.brevo.com', 401, 'Unauthorized', {},
                                       io.BytesIO(b'{"message":"Key not found"}'))
        with mock.patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(RuntimeError) as ctx:
                send_mail('x', 'y', None, ['a@example.com'])
        self.assertIn('401', str(ctx.exception))
        self.assertIn('Key not found', str(ctx.exception))