import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from google.adk.models import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import ValidationError

from .adk_agents import SkillProfile, extract_skills
from .llm import LLMError, MatchResult, ScreeningResult, generate_structured
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


def ai_ok(score=85, match=True):
    """Fake generate_structured: returns a valid AI answer for each schema."""
    def fake(prompt, schema, **kw):
        if schema is ScreeningResult:
            return ScreeningResult(score=score, reason='Strong Django and SQL evidence')
        return MatchResult(is_match=match, match_percent=90 if match else 20, reason='Skills overlap')
    return fake


class PipelineTests(BaseAPITest):
    def setUp(self):
        super().setUp()
        self.login()

    def run_pipeline(self, c, j):
        return self.client.post('/api/run-pipeline/', {'candidate_id': c.id, 'job_id': j.id}, format='json')

    def patch_ai(self, **kw):
        p = mock.patch('api.agents.generate_structured', **kw)
        p.start()
        self.addCleanup(p.stop)

    def test_unknown_ids_return_404(self):
        r = self.client.post('/api/run-pipeline/', {'candidate_id': 999, 'job_id': 999}, format='json')
        self.assertEqual(r.status_code, 404)

    def test_missing_fields_return_400(self):
        r = self.client.post('/api/run-pipeline/', {'candidate_id': 1}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('job_id', r.data)

    def test_ai_success_stores_reason_and_no_fallback_flag(self):
        self.patch_ai(side_effect=ai_ok(score=85))
        c, j = make_candidate(), make_job()
        r = self.run_pipeline(c, j)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['score'], 85)
        self.assertFalse(r.data['fallback_used'])
        self.assertIn('Strong Django', r.data['reason'])
        app = Application.objects.get(candidate=c, job=j)
        self.assertFalse(app.fallback_used)
        self.assertIn('Strong Django', app.reason)
        self.assertEqual(app.status, 'interview_scheduled')

    def test_ai_failure_uses_fallback_and_is_flagged(self):
        self.patch_ai(side_effect=LLMError('quota exceeded'))
        c, j = make_candidate(), make_job()
        r = self.run_pipeline(c, j)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['fallback_used'])
        self.assertIn('AI unavailable', r.data['reason'])
        self.assertTrue(Application.objects.get(candidate=c, job=j).fallback_used)

    def test_fallback_score_never_exceeds_100(self):
        self.patch_ai(side_effect=LLMError('down'))
        c = make_candidate(skills=','.join(f's{i}' for i in range(30)), experience=40)
        r = self.run_pipeline(c, make_job())
        self.assertLessEqual(r.data['score'], 100)

    def test_ai_low_score_rejects_without_interview(self):
        self.patch_ai(side_effect=ai_ok(score=20))
        c, j = make_candidate(), make_job()
        r = self.run_pipeline(c, j)
        self.assertEqual(r.data['status'], 'rejected')
        self.assertIsNone(r.data['interview_slot'])

    def test_ai_no_match_rejects(self):
        self.patch_ai(side_effect=ai_ok(score=80, match=False))
        c, j = make_candidate(), make_job()
        r = self.run_pipeline(c, j)
        self.assertEqual(r.data['status'], 'rejected')

    def test_weak_candidate_rejected_by_fallback(self):
        self.patch_ai(side_effect=LLMError('down'))
        c = make_candidate(skills='Excel', experience=0, resume_text='data entry')
        r = self.run_pipeline(c, make_job())
        self.assertEqual(r.data['status'], 'rejected')
        self.assertIsNone(r.data['interview_slot'])

    def test_pipeline_is_rate_limited(self):
        self.patch_ai(side_effect=LLMError('down'))
        c, j = make_candidate(), make_job()
        codes = [self.run_pipeline(c, j).status_code for _ in range(12)]
        self.assertIn(429, codes)


class LLMWrapperTests(BaseAPITest):
    def _client_returning(self, **resp_attrs):
        client = mock.Mock()
        client.models.generate_content.return_value = mock.Mock(**resp_attrs)
        return client

    def test_returns_parsed_object(self):
        parsed = ScreeningResult(score=70, reason='ok')
        with mock.patch('api.llm._get_client', return_value=self._client_returning(parsed=parsed, text='')):
            self.assertEqual(generate_structured('p', ScreeningResult).score, 70)

    def test_falls_back_to_json_text(self):
        with mock.patch('api.llm._get_client',
                        return_value=self._client_returning(parsed=None, text='{"score": 55, "reason": "fine"}')):
            self.assertEqual(generate_structured('p', ScreeningResult).score, 55)

    def test_score_is_clamped_to_0_100(self):
        self.assertEqual(ScreeningResult(score=250, reason='x').score, 100)
        self.assertEqual(ScreeningResult(score=-5, reason='x').score, 0)

    def test_invalid_json_raises_llmerror(self):
        with mock.patch('api.llm._get_client', return_value=self._client_returning(parsed=None, text='not json')):
            with self.assertRaises(LLMError):
                generate_structured('p', ScreeningResult)

    def test_api_exception_raises_llmerror(self):
        client = mock.Mock()
        client.models.generate_content.side_effect = RuntimeError('503 unavailable')
        with mock.patch('api.llm._get_client', return_value=client):
            with self.assertRaises(LLMError):
                generate_structured('p', ScreeningResult)

    def test_missing_api_key_raises_llmerror(self):
        import api.llm as llm
        with mock.patch.object(llm, '_client', None), self.settings(GEMINI_API_KEY=''):
            with self.assertRaises(LLMError):
                generate_structured('p', ScreeningResult)


class StubLlm(BaseLlm):
    """Offline stand-in for Gemini so ADK can be tested without network."""
    model: str = 'stub'
    reply: str = ''

    @classmethod
    def supported_models(cls):
        return ['stub']

    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(content=types.Content(role='model', parts=[types.Part(text=self.reply)]))


class ADKTests(BaseAPITest):
    def test_adk_agent_runs_from_sync_django_and_returns_structured_output(self):
        stub = StubLlm(reply='{"skills": ["python", "django"], "years_experience": 3, "summary": "Backend dev"}')
        profile = extract_skills('Rahul, 3 years Python and Django', model=stub)
        self.assertIsInstance(profile, SkillProfile)
        self.assertEqual(profile.skills, ['python', 'django'])
        self.assertEqual(profile.years_experience, 3)

    def test_adk_bad_model_output_is_rejected(self):
        stub = StubLlm(reply='{"skills": "not-a-list"}')
        with self.assertRaises(ValidationError):
            extract_skills('resume', model=stub)


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