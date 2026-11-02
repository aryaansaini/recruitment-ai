"""A password change (forgot-password reset) signs the account out on every device."""
import re

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

PW, NEW_PW = 'Str0ng-Pass-123', 'N3w-Better-Pass-987'


class TokenRevocationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('alice', 'a@x.com', PW)
        r = self.client.post('/api/auth/login/', {'username': 'alice', 'password': PW}, format='json')
        self.old_access, self.old_refresh = r.data['access'], r.data['refresh']

    def whoami(self, access):
        return APIClient().get('/api/jobs/', HTTP_AUTHORIZATION='Bearer ' + access).status_code

    def refresh(self, token):
        return APIClient().post('/api/auth/refresh/', {'refresh': token}, format='json')

    def reset_password_via_email(self):
        APIClient().post('/api/auth/forgot-password/', {'email': 'a@x.com'}, format='json')
        code = re.search(r'(\d{6})', mail.outbox[-1].body).group(1)
        return APIClient().post('/api/auth/reset-password/',
                                {'email': 'a@x.com', 'code': code, 'new_password': NEW_PW}, format='json')

    def test_tokens_work_before_any_password_change(self):
        self.assertEqual(self.whoami(self.old_access), 200)
        r = self.refresh(self.old_refresh)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.whoami(r.data['access']), 200)

    def test_reset_revokes_old_refresh_and_access_tokens(self):
        self.assertEqual(self.reset_password_via_email().status_code, 200)
        self.assertEqual(self.refresh(self.old_refresh).status_code, 401)
        self.assertEqual(self.whoami(self.old_access), 401)

    def test_login_with_new_password_gives_working_tokens(self):
        self.reset_password_via_email()
        r = self.client.post('/api/auth/login/', {'username': 'alice', 'password': NEW_PW}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.whoami(r.data['access']), 200)
        self.assertEqual(self.refresh(r.data['refresh']).status_code, 200)

    def test_every_device_is_signed_out(self):
        second = self.client.post('/api/auth/login/', {'username': 'alice', 'password': PW}, format='json').data
        self.reset_password_via_email()
        self.assertEqual(self.refresh(second['refresh']).status_code, 401)
        self.assertEqual(self.whoami(second['access']), 401)

    def test_other_users_are_not_signed_out(self):
        User.objects.create_user('bob', 'b@x.com', PW)
        bob = self.client.post('/api/auth/login/', {'username': 'bob', 'password': PW}, format='json').data
        self.reset_password_via_email()
        self.assertEqual(self.refresh(bob['refresh']).status_code, 200)
        self.assertEqual(self.whoami(bob['access']), 200)

    def test_any_password_change_revokes_not_only_the_reset_flow(self):
        self.user.set_password(NEW_PW)
        self.user.save()
        self.assertEqual(self.refresh(self.old_refresh).status_code, 401)

    def test_tokens_minted_in_code_carry_the_fingerprint(self):
        token = RefreshToken.for_user(self.user)
        self.assertEqual(self.refresh(str(token)).status_code, 200)

    def test_refresh_for_deleted_user_is_401_not_500(self):
        self.user.delete()
        self.assertEqual(self.refresh(self.old_refresh).status_code, 401)

    def test_refresh_for_deactivated_user_is_401(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.refresh(self.old_refresh).status_code, 401)

    def test_garbage_refresh_token_is_401(self):
        self.assertEqual(self.refresh('not-a-token').status_code, 401)
