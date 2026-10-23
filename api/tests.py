import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from .models import Application, Candidate, Job


def make_candidate(**kw):
    data = dict(name='Rahul', email='rahul@example.com', phone='9876543210',
                skills='Python, Django, SQL', experience=3,
                resume_text='Backend dev with Django and SQL experience')
    data.update(kw)
    return Candidate.objects.create(**data)


def make_job(**kw):
    data = dict(title='Backend Dev', required_skills='Python, Django',
                min_experience=2, description='Build APIs')
    data.update(kw)
    return Job.objects.create(**data)


class BaseAPITest(APITestCase):
    def setUp(self):
        cache.clear()  # reset throttle counters between tests
        self.user = User.objects.create_user('recruiter', password='pass12345')

    def login(self):
        self.client.force_authenticate(self.user)


class AuthTests(BaseAPITest):
    def test_login_returns_tokens(self):
        r = self.client.post('/api/auth/login/', {'username': 'recruiter', 'password': 'pass12345'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertIn('access', r.data)
        self.assertIn('refresh', r.data)

    def test_login_wrong_password_is_401(self):
        r = self.client.post('/api/auth/login/', {'username': 'recruiter', 'password': 'wrong'}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_token_grants_access(self):
        r = self.client.post('/api/auth/login/', {'username': 'recruiter', 'password': 'pass12345'}, format='json')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.data['access'])
        self.assertEqual(self.client.get('/api/candidates/').status_code, 200)

    def test_garbage_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer not-a-real-token')
        self.assertEqual(self.client.get('/api/candidates/').status_code, 401)

    def test_login_is_rate_limited(self):
        codes = [self.client.post('/api/auth/login/', {'username': 'recruiter', 'password': 'bad'},
                                  format='json').status_code for _ in range(12)]
        self.assertIn(429, codes)


class ProtectionTests(BaseAPITest):
    def test_all_data_endpoints_require_login(self):
        for url in ['/api/candidates/', '/api/jobs/', '/api/logs/']:
            self.assertEqual(self.client.get(url).status_code, 401, url)
        for url in ['/api/add-job/', '/api/add-candidate/', '/api/run-pipeline/', '/api/parse-resume/']:
            self.assertEqual(self.client.post(url, {}, format='json').status_code, 401, url)

    def test_homepage_is_public(self):
        self.assertEqual(self.client.get('/').status_code, 200)


class CreateTests(BaseAPITest):
    def setUp(self):
        super().setUp()
        self.login()

    def test_add_job_success(self):
        r = self.client.post('/api/add-job/', {
            'title': 'Dev', 'required_skills': 'Python', 'min_experience': '2', 'description': 'x'}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertIn('id', r.data)

    def test_add_job_rejects_negative_experience_and_missing_title(self):
        r = self.client.post('/api/add-job/', {
            'required_skills': 'Python', 'min_experience': -1, 'description': 'x'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('title', r.data)
        self.assertIn('min_experience', r.data)

    def test_add_candidate_success(self):
        r = self.client.post('/api/add-candidate/', {
            'name': 'A', 'email': 'a@b.com', 'phone': '123', 'skills': 'Python',
            'experience': '3', 'resume_text': 'resume'}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Candidate.objects.count(), 1)

    def test_add_candidate_rejects_bad_email_and_empty_resume(self):
        r = self.client.post('/api/add-candidate/', {
            'name': 'A', 'email': 'not-an-email', 'phone': '123', 'skills': 'Python',
            'experience': '3', 'resume_text': ''}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('email', r.data)
        self.assertIn('resume_text', r.data)
        self.assertEqual(Candidate.objects.count(), 0)


class PipelineTests(BaseAPITest):
    def setUp(self):
        super().setUp()
        self.login()
        failing_model = mock.Mock()
        failing_model.generate_content.side_effect = Exception('no network in tests')
        patcher = mock.patch('api.agents.model', failing_model)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unknown_ids_return_404(self):
        r = self.client.post('/api/run-pipeline/', {'candidate_id': 999, 'job_id': 999}, format='json')
        self.assertEqual(r.status_code, 404)

    def test_missing_fields_return_400(self):
        r = self.client.post('/api/run-pipeline/', {'candidate_id': 1}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('job_id', r.data)

    def test_pipeline_success_creates_application(self):
        c, j = make_candidate(), make_job()
        r = self.client.post('/api/run-pipeline/', {'candidate_id': c.id, 'job_id': j.id}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'interview_scheduled')
        self.assertIsNotNone(r.data['interview_slot'])
        app = Application.objects.get(candidate=c, job=j)
        self.assertEqual(app.status, 'interview_scheduled')

    def test_weak_candidate_is_rejected_without_interview(self):
        c = make_candidate(skills='Excel', experience=0, resume_text='data entry')
        j = make_job()
        r = self.client.post('/api/run-pipeline/', {'candidate_id': c.id, 'job_id': j.id}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'rejected')
        self.assertIsNone(r.data['interview_slot'])

    def test_pipeline_is_rate_limited(self):
        c, j = make_candidate(), make_job()
        codes = [self.client.post('/api/run-pipeline/', {'candidate_id': c.id, 'job_id': j.id},
                                  format='json').status_code for _ in range(12)]
        self.assertIn(429, codes)


class ParseResumeTests(BaseAPITest):
    def setUp(self):
        super().setUp()
        self.login()

    def test_no_file_is_400(self):
        self.assertEqual(self.client.post('/api/parse-resume/', {}, format='multipart').status_code, 400)

    def test_non_pdf_is_400(self):
        f = SimpleUploadedFile('resume.exe', b'MZ...', content_type='application/octet-stream')
        self.assertEqual(self.client.post('/api/parse-resume/', {'resume': f}, format='multipart').status_code, 400)

    def test_oversized_file_is_400(self):
        f = SimpleUploadedFile('big.pdf', b'0' * (5 * 1024 * 1024 + 1), content_type='application/pdf')
        self.assertEqual(self.client.post('/api/parse-resume/', {'resume': f}, format='multipart').status_code, 400)

    def test_corrupt_pdf_is_422(self):
        f = SimpleUploadedFile('bad.pdf', b'this is not a pdf', content_type='application/pdf')
        self.assertEqual(self.client.post('/api/parse-resume/', {'resume': f}, format='multipart').status_code, 422)


class ModelTests(BaseAPITest):
    def test_one_application_per_candidate_job_pair(self):
        c, j = make_candidate(), make_job()
        Application.objects.create(candidate=c, job=j)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Application.objects.create(candidate=c, job=j)

    def test_candidate_can_apply_to_multiple_jobs(self):
        c = make_candidate()
        Application.objects.create(candidate=c, job=make_job(title='A'))
        Application.objects.create(candidate=c, job=make_job(title='B'))
        self.assertEqual(c.applications.count(), 2)