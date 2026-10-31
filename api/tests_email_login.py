from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()
PASSWORD = 'Str0ng!pass-93'


class EmailLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('riya_hr', email='riya@example.com', password=PASSWORD)
        self.c = APIClient()

    def login(self, who, password=PASSWORD):
        return self.c.post('/api/auth/login/', {'username': who, 'password': password}, format='json')

    def test_username_login_still_works(self):
        self.assertEqual(self.login('riya_hr').status_code, 200)

    def test_email_login_works_in_any_letter_case(self):
        self.assertEqual(self.login('riya@example.com').status_code, 200)
        self.assertEqual(self.login('Riya@Example.com').status_code, 200)

    def test_wrong_password_is_rejected_for_email_too(self):
        self.assertEqual(self.login('riya@example.com', 'Wrong!pass-0000').status_code, 401)

    def test_unknown_email_is_rejected(self):
        self.assertEqual(self.login('nobody@example.com').status_code, 401)

    def test_inactive_account_cannot_sign_in_by_email(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.login('riya@example.com').status_code, 401)

    def test_two_accounts_sharing_an_email_cannot_use_it_to_sign_in(self):
        User.objects.create_user('someone_else', email='riya@example.com', password=PASSWORD)
        self.assertEqual(self.login('riya@example.com').status_code, 401)
        self.assertEqual(self.login('riya_hr').status_code, 200)

    def test_token_from_email_login_belongs_to_the_right_account(self):
        access = self.login('riya@example.com').json()['access']
        self.c.credentials(HTTP_AUTHORIZATION='Bearer ' + access)
        self.assertEqual(self.c.get('/api/auth/profile/').json()['username'], 'riya_hr')