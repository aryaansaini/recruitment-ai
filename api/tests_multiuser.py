"""Multi-user tests: sign-up with phone, per-recruiter data isolation, read-only demo, limits, seed command."""
import importlib
from io import StringIO
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import override_settings
from rest_framework.test import APITestCase

from .llm import LLMError
from .management.commands.seed_demo import CANDIDATES, JOBS
from .models import AgentLog, Application, Candidate, Job, RecruiterProfile, ResumeChunk
from .resume_parser import split_sections
from .services import embed_candidate_chunks, rebuild_resume_chunks
from .tests import SAMPLE_RESUME
from .tests_embeddings import fake_embed_batch
from .throttles import PipelineDailyThrottle

GOOD = {'username': 'newrecruiter', 'phone': '+91 98765 43210', 'password': 'Tr1cky!Passw0rd'}


def candidate_for(owner, **kw):
    data = dict(name='Rahul', email='r@example.com', phone='123', skills='Python, Django', experience=3,
                resume_text=SAMPLE_RESUME, owner=owner)
    data.update(kw)
    return Candidate.objects.create(**data)


def job_for(owner, **kw):
    data = dict(title='Backend', required_skills='Python', min_experience=1, description='APIs', owner=owner)
    data.update(kw)
    return Job.objects.create(**data)


class Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user('alice', password='Str0ng!pass#1')
        self.bob = User.objects.create_user('bob', password='Str0ng!pass#2')

    def signup(self, **kw):
        body = dict(GOOD)
        body.update(kw)
        return self.client.post('/api/auth/signup/', body, format='json')

    def as_user(self, user):
        self.client.force_authenticate(user)


class SignupTests(Base):


    def test_invalid_phone_numbers_are_rejected(self):
        for bad in ['abc', '12345', '+1234567890123456', '98765-4321x', '']:
            self.assertEqual(self.signup(phone=bad, username='user' + str(abs(hash(bad)) % 1000)).status_code, 400, bad)




    def test_password_too_similar_to_username_is_rejected(self):
        self.assertEqual(self.signup(username='rahulsharma', password='rahulsharma1').status_code, 400)

    def test_username_rules(self):
        self.assertEqual(self.signup(username='ALICE').status_code, 400)                # case-insensitive duplicate
        for name in ['admin', 'demo_recruiter', 'a', 'has space', 'x' * 31]:
            self.assertEqual(self.signup(username=name, phone='+919800000000').status_code, 400, name)

    def test_signups_can_be_switched_off(self):
        with override_settings(SIGNUPS_ENABLED=False):
            r = self.signup()
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.filter(username='newrecruiter').exists())

    

    def test_global_daily_signup_cap(self):
        with override_settings(MAX_SIGNUPS_PER_DAY=2):                     # alice and bob already joined today
            r = self.signup()
        self.assertEqual(r.status_code, 403)
        self.assertIn('daily', r.data['detail'])

    def test_signup_is_rate_limited_per_ip(self):
        codes = [self.client.post('/api/auth/signup/', {}, format='json').status_code for _ in range(12)]
        self.assertIn(429, codes)

        @override_settings(EMAIL_OTP_REQUIRED=False)
        def test_auth_config_is_public(self):
            r = self.client.get('/api/auth/config/')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, {'signups_enabled': True, 'invite_required': False, 'email_otp_required': False})
            with override_settings(SIGNUPS_ENABLED=False, SIGNUP_INVITE_CODE='x'):
                self.assertEqual(self.client.get('/api/auth/config/').data, {'signups_enabled': False, 'invite_required': True, 'email_otp_required': False})

class IsolationTests(Base):
    def setUp(self):
        super().setUp()
        self.ca, self.ja = candidate_for(self.alice, name='Alice Cand'), job_for(self.alice, title='Alice Job')
        self.cb, self.jb = candidate_for(self.bob, name='Bob Cand', email='b@example.com'), job_for(self.bob, title='Bob Job')

    def test_lists_only_show_own_data(self):
        self.as_user(self.alice)
        self.assertEqual([c['name'] for c in self.client.get('/api/candidates/').data], ['Alice Cand'])
        self.assertEqual([j['title'] for j in self.client.get('/api/jobs/').data], ['Alice Job'])
        self.as_user(self.bob)
        self.assertEqual([c['name'] for c in self.client.get('/api/candidates/').data], ['Bob Cand'])

    def test_logs_only_show_own_data(self):
        AgentLog.objects.create(candidate=self.ca, from_agent='A', to_agent='B', message='alice log')
        AgentLog.objects.create(candidate=self.cb, from_agent='A', to_agent='B', message='bob log')
        self.as_user(self.alice)
        self.assertEqual([l['message'] for l in self.client.get('/api/logs/').data], ['alice log'])

    def test_logs_are_capped_and_ordered_oldest_first(self):
        AgentLog.objects.bulk_create([AgentLog(candidate=self.ca, from_agent='A', to_agent='B', message=str(i)) for i in range(205)])
        self.as_user(self.alice)
        logs = self.client.get('/api/logs/').data
        self.assertEqual(len(logs), 200)
        self.assertEqual((logs[0]['message'], logs[-1]['message']), ('5', '204'))

    def test_cannot_run_pipeline_on_someone_elses_candidate_or_job(self):
        self.as_user(self.alice)
        for body in ({'candidate_id': self.cb.id, 'job_id': self.ja.id}, {'candidate_id': self.ca.id, 'job_id': self.jb.id}):
            self.assertEqual(self.client.post('/api/run-pipeline/', body, format='json').status_code, 404)
        self.assertEqual(Application.objects.count(), 0)

    def test_cannot_delete_someone_elses_data(self):
        self.as_user(self.alice)
        self.assertEqual(self.client.delete(f'/api/candidates/{self.cb.id}/').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/jobs/{self.jb.id}/').status_code, 404)
        self.assertTrue(Candidate.objects.filter(pk=self.cb.id).exists() and Job.objects.filter(pk=self.jb.id).exists())

    def test_owner_always_comes_from_the_login_not_the_payload(self):
        self.as_user(self.alice)
        r = self.client.post('/api/add-candidate/', {'name': 'X', 'email': 'x@example.com', 'phone': '1', 'skills': 'Python',
                                                     'experience': 1, 'resume_text': 'resume', 'owner': self.bob.id}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Candidate.objects.get(pk=r.data['id']).owner, self.alice)
        r = self.client.post('/api/add-job/', {'title': 'T', 'required_skills': 'x', 'min_experience': 1,
                                               'description': 'd', 'owner': self.bob.id}, format='json')
        self.assertEqual(Job.objects.get(pk=r.data['id']).owner, self.alice)

    def test_semantic_search_only_returns_own_candidates(self):
        with mock.patch('api.embeddings._embed_batch', side_effect=fake_embed_batch):
            for c in (self.ca, self.cb):                                    # identical resumes, different owners
                rebuild_resume_chunks(c)
                embed_candidate_chunks(c)
            self.as_user(self.alice)
            r = self.client.get('/api/search/?q=Django REST APIs&limit=10')
        self.assertEqual([x['name'] for x in r.data], ['Alice Cand'])

    def test_staff_also_only_sees_their_own_data_through_the_api(self):
        boss = User.objects.create_superuser('boss', password='Str0ng!pass#3')
        self.as_user(boss)
        self.assertEqual(self.client.get('/api/candidates/').data, [])


class DemoWorkspaceTests(Base):
    def setUp(self):
        super().setUp()
        self.demo = User.objects.create_user('demo_recruiter')
        self.demo_cand, self.demo_job = candidate_for(self.demo, name='Demo Cand'), job_for(self.demo)

    def test_demo_login_works_and_sees_only_demo_data(self):
        r = self.client.post('/api/auth/demo/')
        self.assertEqual(r.status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.data['access'])
        self.assertEqual([c['name'] for c in self.client.get('/api/candidates/').data], ['Demo Cand'])

    def test_normal_users_never_see_demo_data(self):
        self.as_user(self.alice)
        self.assertEqual(self.client.get('/api/candidates/').data, [])

    def test_demo_is_read_only(self):
        self.as_user(self.demo)
        cand = {'name': 'X', 'email': 'x@example.com', 'phone': '1', 'skills': 'a', 'experience': 1, 'resume_text': 'r'}
        self.assertEqual(self.client.post('/api/add-candidate/', cand, format='json').status_code, 403)
        self.assertEqual(self.client.post('/api/add-job/', {'title': 'T', 'required_skills': 'x', 'min_experience': 1, 'description': 'd'}, format='json').status_code, 403)
        self.assertEqual(self.client.delete(f'/api/candidates/{self.demo_cand.id}/').status_code, 403)
        self.assertEqual(self.client.delete(f'/api/jobs/{self.demo_job.id}/').status_code, 403)
        f = SimpleUploadedFile('cv.txt', b'hello', content_type='text/plain')
        self.assertEqual(self.client.post('/api/parse-resume/', {'resume': f}, format='multipart').status_code, 403)
        self.assertTrue(Candidate.objects.filter(pk=self.demo_cand.id).exists())

    def test_demo_can_still_run_the_pipeline(self):
        self.as_user(self.demo)
        with mock.patch('api.agents.generate_structured', side_effect=LLMError('down')):
            r = self.client.post('/api/run-pipeline/', {'candidate_id': self.demo_cand.id, 'job_id': self.demo_job.id}, format='json')
        self.assertEqual(r.status_code, 200)

    def test_demo_account_cannot_be_deleted(self):
        self.as_user(self.demo)
        self.assertEqual(self.client.delete('/api/auth/me/', {'password': 'x'}, format='json').status_code, 403)
        self.assertTrue(User.objects.filter(username='demo_recruiter').exists())

    def test_demo_has_no_usable_password(self):
        self.client.post('/api/auth/demo/')
        self.assertFalse(User.objects.get(username='demo_recruiter').has_usable_password())


class QuotaTests(Base):
    def test_candidate_limit_per_account_and_staff_is_exempt(self):
        self.as_user(self.alice)
        body = {'name': 'X', 'email': 'x@example.com', 'phone': '1', 'skills': 'a', 'experience': 1, 'resume_text': 'r'}
        with override_settings(MAX_CANDIDATES_PER_USER=2):
            self.assertEqual([self.client.post('/api/add-candidate/', body, format='json').status_code for _ in range(3)], [201, 201, 403])
            self.as_user(User.objects.create_superuser('boss', password='Str0ng!pass#3'))
            self.assertEqual([self.client.post('/api/add-candidate/', body, format='json').status_code for _ in range(3)], [201, 201, 201])

    def test_job_limit_per_account(self):
        self.as_user(self.alice)
        body = {'title': 'T', 'required_skills': 'x', 'min_experience': 1, 'description': 'd'}
        with override_settings(MAX_JOBS_PER_USER=1):
            self.assertEqual([self.client.post('/api/add-job/', body, format='json').status_code for _ in range(2)], [201, 403])

    def test_deleting_frees_the_quota_and_removes_resume_data(self):
        c = candidate_for(self.alice)
        rebuild_resume_chunks(c)
        Application.objects.create(candidate=c, job=job_for(self.alice))
        self.as_user(self.alice)
        with override_settings(MAX_CANDIDATES_PER_USER=1):
            self.assertEqual(self.client.delete(f'/api/candidates/{c.id}/').status_code, 204)
            r = self.client.post('/api/add-candidate/', {'name': 'N', 'email': 'n@example.com', 'phone': '1', 'skills': 'a',
                                                         'experience': 1, 'resume_text': 'r'}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual((ResumeChunk.objects.filter(candidate_id=c.id).count(), Application.objects.count()), (0, 0))

    def test_daily_pipeline_limit_protects_the_shared_gemini_quota(self):
        c, j = candidate_for(self.alice), job_for(self.alice)
        self.as_user(self.alice)
        with mock.patch.dict(PipelineDailyThrottle.THROTTLE_RATES, {'pipeline_day': '2/day'}), \
                mock.patch('api.agents.generate_structured', side_effect=LLMError('down')):
            codes = [self.client.post('/api/run-pipeline/', {'candidate_id': c.id, 'job_id': j.id}, format='json').status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


class AccountTests(Base):
    def test_me_shows_usage_limits_and_phone(self):
        RecruiterProfile.objects.create(user=self.alice, phone='+919876543210')
        candidate_for(self.alice)
        self.as_user(self.alice)
        d = self.client.get('/api/auth/me/').data
        self.assertEqual((d['username'], d['phone'], d['usage']['candidates'], d['is_demo']), ('alice', '+919876543210', 1, False))
        self.assertIn('candidates', d['limits'])

    def test_me_requires_login(self):
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 401)
        self.assertEqual(self.client.delete('/api/auth/me/').status_code, 401)

    def test_delete_account_needs_the_right_password(self):
        self.as_user(self.alice)
        self.assertEqual(self.client.delete('/api/auth/me/', {'password': 'wrong'}, format='json').status_code, 400)
        self.assertTrue(User.objects.filter(username='alice').exists())

    def test_delete_account_removes_everything_of_that_user_only(self):
        RecruiterProfile.objects.create(user=self.alice, phone='+919876543210')
        ca, ja = candidate_for(self.alice), job_for(self.alice)
        rebuild_resume_chunks(ca)
        Application.objects.create(candidate=ca, job=ja)
        AgentLog.objects.create(candidate=ca, from_agent='A', to_agent='B', message='m')
        cb = candidate_for(self.bob)
        rebuild_resume_chunks(cb)
        alice_id = self.alice.id
        self.as_user(self.alice)
        r = self.client.delete('/api/auth/me/', {'password': 'Str0ng!pass#1'}, format='json')
        self.assertEqual(r.status_code, 204)
        self.assertFalse(User.objects.filter(username='alice').exists())
        self.assertEqual((Candidate.objects.filter(owner_id=alice_id).count(), Job.objects.count(), Application.objects.count(),
                          AgentLog.objects.count(), RecruiterProfile.objects.count()), (0, 0, 0, 0, 0))
        self.assertEqual(ResumeChunk.objects.filter(candidate=ca).count(), 0)
        self.assertTrue(ResumeChunk.objects.filter(candidate=cb).exists())      # bob is untouched

    def test_staff_account_cannot_be_deleted_through_the_api(self):
        boss = User.objects.create_superuser('boss', password='Str0ng!pass#3')
        self.as_user(boss)
        self.assertEqual(self.client.delete('/api/auth/me/', {'password': 'Str0ng!pass#3'}, format='json').status_code, 403)


class OwnerMigrationTests(Base):
    def test_existing_rows_are_assigned_to_the_first_superuser(self):
        migration = importlib.import_module('api.migrations.0007_multi_user')
        boss = User.objects.create_superuser('boss', password='Str0ng!pass#3')
        c, j = candidate_for(None), job_for(None)
        mine = candidate_for(self.alice, email='m@example.com')
        migration.assign_existing_data(django_apps, None)
        c.refresh_from_db(); j.refresh_from_db(); mine.refresh_from_db()
        self.assertEqual((c.owner, j.owner, mine.owner), (boss, boss, self.alice))   # already-owned rows are left alone

    def test_without_any_user_nothing_happens(self):
        migration = importlib.import_module('api.migrations.0007_multi_user')
        User.objects.all().delete()
        candidate_for(None)
        migration.assign_existing_data(django_apps, None)
        self.assertIsNone(Candidate.objects.get().owner)


class SeedDemoTests(Base):
    def run_seed(self, *args):
        out = StringIO()
        call_command('seed_demo', *args, stdout=out)
        return out.getvalue()

    def test_creates_the_demo_workspace(self):
        out = self.run_seed('--no-embed')
        demo = User.objects.get(username='demo_recruiter')
        self.assertFalse(demo.has_usable_password())
        self.assertEqual((Job.objects.filter(owner=demo).count(), Candidate.objects.filter(owner=demo).count()), (len(JOBS), len(CANDIDATES)))
        self.assertTrue(all(c.chunks.count() >= 4 for c in Candidate.objects.filter(owner=demo)))
        self.assertIn('Demo workspace ready', out)

    def test_demo_data_is_clearly_fictional_and_well_formed(self):
        emails = [c[1] for c in CANDIDATES]
        self.assertEqual(len(emails), len(set(emails)))
        self.assertTrue(all(e.endswith('@example.com') for e in emails))
        self.assertTrue(all(c[2].startswith('+1-555-01') for c in CANDIDATES))
        for name, _e, _p, _skills, _years, resume in CANDIDATES:
            sections = split_sections(resume)
            self.assertIn('skills', sections, name)
            self.assertIn('education', sections, name)

    def test_running_twice_does_not_duplicate(self):
        self.run_seed('--no-embed')
        self.run_seed('--no-embed')
        self.assertEqual(Candidate.objects.count(), len(CANDIDATES))
        self.assertEqual(Job.objects.count(), len(JOBS))

    def test_other_users_data_is_not_touched_by_default(self):
        candidate_for(self.alice)
        self.run_seed('--no-embed')
        self.assertEqual(Candidate.objects.filter(owner=self.alice).count(), 1)

    def test_wipe_user_deletes_only_that_users_data(self):
        candidate_for(self.alice), job_for(self.alice), candidate_for(self.bob, email='b@example.com')
        self.run_seed('--no-embed', '--wipe-user', 'alice', '--noinput')
        self.assertEqual((Candidate.objects.filter(owner=self.alice).count(), Job.objects.filter(owner=self.alice).count()), (0, 0))
        self.assertEqual(Candidate.objects.filter(owner=self.bob).count(), 1)

    def test_wipe_all_deletes_everyones_old_data(self):
        candidate_for(self.alice), candidate_for(self.bob, email='b@example.com'), job_for(self.bob)
        self.run_seed('--no-embed', '--wipe-all', '--noinput')
        self.assertEqual(Candidate.objects.exclude(owner__username='demo_recruiter').count(), 0)
        self.assertEqual(Job.objects.exclude(owner__username='demo_recruiter').count(), 0)
        self.assertEqual(Candidate.objects.count(), len(CANDIDATES))

    def test_wipe_asks_first_and_cancelling_deletes_nothing(self):
        candidate_for(self.alice)
        with mock.patch('builtins.input', return_value='no'):
            with self.assertRaises(CommandError):
                self.run_seed('--no-embed', '--wipe-all')
        self.assertEqual(Candidate.objects.filter(owner=self.alice).count(), 1)

    def test_wipe_unknown_user_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_seed('--no-embed', '--wipe-user', 'nobody', '--noinput')

    def test_embeddings_are_created_when_available(self):
        with mock.patch('api.embeddings._embed_batch', side_effect=fake_embed_batch):
            out = self.run_seed()
        self.assertEqual(ResumeChunk.objects.filter(embedding__isnull=True).count(), 0)
        self.assertIn('resume chunks embedded', out)

    def test_seed_still_works_when_the_embedding_service_is_down(self):
        from .embeddings import EmbeddingError
        with mock.patch('api.embeddings._embed_batch', side_effect=EmbeddingError('down')):
            self.run_seed()
        self.assertEqual(Candidate.objects.count(), len(CANDIDATES))

    def test_score_runs_the_pipeline_for_the_demo_candidates(self):
        with mock.patch('api.agents.generate_structured', side_effect=LLMError('down')):
            out = self.run_seed('--no-embed', '--score')
        self.assertEqual(Application.objects.count(), 8)
        self.assertTrue(Candidate.objects.filter(score__gt=0).exists())
        self.assertIn('AI fallback', out)