"""Deployment tests: health check, HTTPS redirect behind Render's proxy, host/CSRF settings from env."""
import json
import os
import subprocess
import sys

from django.test import Client, SimpleTestCase, TestCase, override_settings


class HealthCheckTests(TestCase):
    def test_healthz_is_public_and_returns_ok(self):
        r = Client().get('/healthz')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'status': 'ok'})

    def test_healthz_uses_no_database(self):
        with self.assertNumQueries(0):
            Client().get('/healthz')

    def test_healthz_rejects_post(self):
        self.assertEqual(Client().post('/healthz').status_code, 405)


@override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r'^healthz$'], ALLOWED_HOSTS=['testserver'])
class HttpsBehindProxyTests(SimpleTestCase):
    """Render terminates HTTPS and forwards plain http with X-Forwarded-Proto."""

    def test_plain_http_is_redirected_to_https(self):
        r = Client().get('/')
        self.assertEqual(r.status_code, 301)
        self.assertTrue(r['Location'].startswith('https://'))

    def test_request_marked_https_by_the_proxy_is_served(self):
        self.assertEqual(Client().get('/', HTTP_X_FORWARDED_PROTO='https').status_code, 200)

    def test_health_check_is_never_redirected(self):
        self.assertEqual(Client().get('/healthz').status_code, 200)


class SettingsFromEnvTests(SimpleTestCase):
    """Settings are read at import time, so check them in a fresh process."""

    def settings_with(self, **env):
        code = ("import json, django; django.setup(); from django.conf import settings as s; "
                "print(json.dumps([s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS, s.DEBUG]))")
        full_env = {k: v for k, v in os.environ.items() if k not in ('RENDER_EXTERNAL_HOSTNAME', 'ALLOWED_HOSTS', 'CSRF_TRUSTED_ORIGINS')}
        full_env.update({'DJANGO_SETTINGS_MODULE': 'recruitment_project.settings', 'SECRET_KEY': 'x' * 50}, **env)
        out = subprocess.run([sys.executable, '-c', code], env=full_env, capture_output=True, text=True, check=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_render_hostname_is_allowed_and_trusted_for_csrf(self):
        hosts, origins, _debug = self.settings_with(RENDER_EXTERNAL_HOSTNAME='my-app.onrender.com')
        self.assertIn('my-app.onrender.com', hosts)
        self.assertIn('https://my-app.onrender.com', origins)

    def test_without_render_host_only_local_hosts_are_allowed(self):
        hosts, origins, _debug = self.settings_with()
        self.assertEqual(origins, [])
        self.assertNotIn('onrender.com', ' '.join(hosts))

    def test_extra_hosts_and_origins_come_from_env(self):
        hosts, origins, _debug = self.settings_with(ALLOWED_HOSTS='a.com, b.com', CSRF_TRUSTED_ORIGINS='https://a.com')
        self.assertEqual(hosts[:2], ['a.com', 'b.com'])
        self.assertEqual(origins, ['https://a.com'])

    def test_debug_defaults_to_off(self):
        env = {'DEBUG': ''}
        _h, _o, debug = self.settings_with(**env)
        self.assertFalse(debug)