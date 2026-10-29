import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .auth_views import DEMO_USERNAME, TEMP_PREFIX
from .models import Candidate, EmailOTP, Job

User = get_user_model()
PASSWORD = 'Str0ng!pass-93'


@override_settings(SIGNUPS_ENABLED=True, SIGNUP_INVITE_CODE='', MAX_SIGNUPS_PER_DAY=100)
class EmailOtpTests(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()

    def signup(self, **kw):
        data = {'username': 'riya_hr', 'email': 'Riya@Example.com', 'password': PASSWORD}
        data.update(kw)
        return self.c.post('/api/auth/signup/', data, format='json')

    def code(self):
        return re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)

    def verify(self, code, username='riya_hr'):
        return self.c.post('/api/auth/verify-otp/', {'username': username, 'code': code}, format='json')

    def test_signup_creates_inactive_user_and_sends_mail(self):
        r = self.signup()
        self.assertEqual(r.status_code, 201)
        self.assertNotIn('access', r.json())
        user = User.objects.get(username='riya_hr')
        self.assertFalse(user.is_active)
        self.assertEqual(user.email, 'riya@example.com')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['riya@example.com'])
        login = self.c.post('/api/auth/login/', {'username': 'riya_hr', 'password': PASSWORD}, format='json')
        self.assertEqual(login.status_code, 401)

    def test_code_is_stored_hashed(self):
        self.signup()
        self.assertNotIn(self.code(), EmailOTP.objects.get().code_hash)

    def test_right_code_activates_and_logs_in(self):
        self.signup()
        r = self.verify(self.code())
        self.assertEqual(r.status_code, 200)
        self.assertIn('access', r.json())
        self.assertTrue(User.objects.get(username='riya_hr').is_active)
        self.assertEqual(EmailOTP.objects.count(), 0)
        login = self.c.post('/api/auth/login/', {'username': 'riya_hr', 'password': PASSWORD}, format='json')
        self.assertEqual(login.status_code, 200)

    def test_wrong_code_locks_after_max_attempts(self):
        self.signup()
        good = self.code()
        bad = '000000' if good != '000000' else '111111'
        for _ in range(5):
            self.assertEqual(self.verify(bad).status_code, 400)
        self.assertEqual(self.verify(good).status_code, 400)       # locked even with the right code
        self.assertFalse(User.objects.get(username='riya_hr').is_active)

    def test_expired_code_is_rejected(self):
        self.signup()
        EmailOTP.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.verify(self.code()).status_code, 400)

    def test_resend_has_cooldown_and_resets_attempts(self):
        self.signup()
        r = self.c.post('/api/auth/resend-otp/', {'username': 'riya_hr'}, format='json')
        self.assertEqual(r.status_code, 429)
        EmailOTP.objects.update(last_sent_at=timezone.now() - timedelta(minutes=5), attempts=4)
        r = self.c.post('/api/auth/resend-otp/', {'username': 'riya_hr'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(EmailOTP.objects.get().attempts, 0)
        self.assertEqual(self.verify(self.code()).status_code, 200)

    def test_duplicate_active_email_and_reserved_username(self):
        User.objects.create_user('taken', email='riya@example.com', password=PASSWORD)
        self.assertEqual(self.signup().status_code, 400)
        self.assertEqual(self.signup(email='new@example.com', username='demo_boss').status_code, 400)

    def test_unverified_attempt_can_be_retried(self):
        self.signup()
        self.assertEqual(self.signup().status_code, 201)
        self.assertEqual(User.objects.filter(username='riya_hr').count(), 1)

    def test_email_failure_removes_pending_user(self):
        with override_settings(EMAIL_BACKEND='api.nonexistent.Backend'):
            r = self.signup()
        self.assertEqual(r.status_code, 503)
        self.assertFalse(User.objects.filter(username='riya_hr').exists())
    
    @override_settings(SIGNUP_INVITE_CODE='letmein-123')
    def test_invite_code_is_enforced_when_configured(self):
        self.assertEqual(self.signup().status_code, 403)
        self.assertEqual(self.signup(invite_code='wrong').status_code, 403)
        self.assertEqual(self.signup(invite_code='letmein-123').status_code, 201)

    def test_weak_passwords_are_rejected(self):
        for pw in ('short', '12345678', 'password'):
            r = self.signup(password=pw)
            self.assertEqual(r.status_code, 400)
            self.assertIn('password', r.json())
        self.assertFalse(User.objects.filter(username='riya_hr').exists())

class DemoSessionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()
        tpl = User.objects.create_user(DEMO_USERNAME)
        job = Job.objects.create(title='Backend', required_skills='Python', min_experience=1,
                                 description='x', owner=tpl)
        Candidate.objects.create(name='Sample', email='s@example.com', phone='1', skills='Python',
                                 experience=2, resume_text='Python dev', owner=tpl)
        self.job = job

    def start_demo(self):
        r = self.c.post('/api/auth/demo/')
        self.assertEqual(r.status_code, 200)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return client, r.json()['username']

    def test_demo_gets_private_copy_with_full_access(self):
        client, name = self.start_demo()
        self.assertTrue(name.startswith(TEMP_PREFIX))
        self.assertEqual(len(client.get('/api/candidates/').json()), 1)
        add = client.post('/api/add-job/', {'title': 'Mine', 'required_skills': 'Go',
                                            'min_experience': 0, 'description': 'd'}, format='json')
        self.assertIn(add.status_code, (200, 201))
        # the template workspace is untouched
        self.assertEqual(Job.objects.filter(owner__username=DEMO_USERNAME).count(), 1)

    def test_two_visitors_do_not_see_each_other(self):
        a, _ = self.start_demo()
        b, _ = self.start_demo()
        a.post('/api/add-job/', {'title': 'OnlyA', 'required_skills': 'Go',
                                 'min_experience': 0, 'description': 'd'}, format='json')
        titles = [j['title'] for j in b.get('/api/jobs/').json()]
        self.assertNotIn('OnlyA', titles)

    def test_demo_end_deletes_everything(self):
        client, name = self.start_demo()
        self.assertEqual(client.post('/api/auth/demo/end/').status_code, 204)
        self.assertFalse(User.objects.filter(username=name).exists())
        self.assertTrue(User.objects.filter(username=DEMO_USERNAME).exists())
        self.assertEqual(Candidate.objects.filter(owner__username__startswith=TEMP_PREFIX).count(), 0)

    def test_old_demo_accounts_are_cleaned_up(self):
        _, name = self.start_demo()
        User.objects.filter(username=name).update(date_joined=timezone.now() - timedelta(hours=3))
        cache.clear()
        self.start_demo()
        self.assertFalse(User.objects.filter(username=name).exists())

    def test_real_user_cannot_use_demo_end(self):
        User.objects.create_user('real', password=PASSWORD)
        c = APIClient()
        c.force_authenticate(User.objects.get(username='real'))
        self.assertEqual(c.post('/api/auth/demo/end/').status_code, 403)
        self.assertTrue(User.objects.filter(username='real').exists())


class ProfileTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('real', email='r@example.com', password=PASSWORD)
        self.c = APIClient()
        self.c.force_authenticate(self.user)

    def test_profile_counts_and_clear_data(self):
        Job.objects.create(title='J', required_skills='x', min_experience=0, description='d', owner=self.user)
        Candidate.objects.create(name='C', email='c@example.com', phone='1', skills='x', experience=1,
                                 resume_text='t', owner=self.user)
        p = self.c.get('/api/auth/profile/').json()
        self.assertEqual((p['jobs'], p['candidates'], p['email']), (1, 1, 'r@example.com'))
        self.assertEqual(self.c.post('/api/auth/clear-data/').status_code, 200)
        self.assertEqual(self.c.get('/api/auth/profile/').json()['jobs'], 0)
        self.assertTrue(User.objects.filter(username='real').exists())