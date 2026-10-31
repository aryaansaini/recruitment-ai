import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import EmailOTP

User = get_user_model()
OLD, NEW = 'Old!pass-12345', 'New!pass-67890'


@override_settings(EMAIL_OTP_REQUIRED=False)
class ForgotPasswordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('riya_hr', email='riya@example.com', password=OLD)
        self.c = APIClient()

    def forgot(self, email='riya@example.com'):
        return self.c.post('/api/auth/forgot-password/', {'email': email}, format='json')

    def reset(self, code, password=NEW, email='riya@example.com'):
        return self.c.post('/api/auth/reset-password/',
                           {'email': email, 'code': code, 'new_password': password}, format='json')

    def code(self):
        return re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)

    def can_login(self, password):
        r = self.c.post('/api/auth/login/', {'username': 'riya_hr', 'password': password}, format='json')
        return r.status_code == 200

    def test_code_is_emailed_with_reset_wording(self):
        self.assertEqual(self.forgot().status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('password reset', mail.outbox[0].subject.lower())
        self.assertEqual(mail.outbox[0].to, ['riya@example.com'])

    def test_unknown_email_gets_the_same_answer_and_no_mail(self):
        known = self.forgot().json()
        cache.clear()
        unknown = self.forgot('nobody@example.com')
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.json(), known)
        self.assertEqual(len(mail.outbox), 1)          # only the real account got a mail

    def test_right_code_changes_the_password(self):
        self.forgot()
        self.assertEqual(self.reset(self.code()).status_code, 200)
        self.assertTrue(self.can_login(NEW))
        self.assertFalse(self.can_login(OLD))
        self.assertEqual(EmailOTP.objects.count(), 0)

    def test_wrong_code_does_not_change_anything_and_locks_after_five_tries(self):
        self.forgot()
        good = self.code()
        bad = '000000' if good != '000000' else '111111'
        for _ in range(5):
            self.assertEqual(self.reset(bad).status_code, 400)
        self.assertEqual(self.reset(good).status_code, 400)       # locked, even with the right code
        self.assertTrue(self.can_login(OLD))

    def test_expired_code_is_rejected(self):
        self.forgot()
        EmailOTP.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.reset(self.code()).status_code, 400)
        self.assertTrue(self.can_login(OLD))

    def test_weak_new_password_is_rejected_and_the_code_stays_usable(self):
        self.forgot()
        code = self.code()
        r = self.reset(code, password='12345678')
        self.assertEqual(r.status_code, 400)
        self.assertIn('new_password', r.json())
        self.assertEqual(self.reset(code).status_code, 200)

    def test_second_request_within_the_cooldown_sends_no_second_mail(self):
        self.forgot()
        cache.clear()
        self.assertEqual(self.forgot().status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_inactive_accounts_are_ignored(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.forgot()
        self.assertEqual(len(mail.outbox), 0)

    def test_email_is_required(self):
        self.assertEqual(self.forgot('').status_code, 400)