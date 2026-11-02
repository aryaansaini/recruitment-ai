"""Candidate detail, application list (Kanban) and status moves."""
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from .models import AgentLog, Application
from .tests_multiuser import candidate_for, job_for


class BoardBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user('alice', password='x-Pass-12345')
        self.bob = User.objects.create_user('bob', password='x-Pass-12345')
        self.ca, self.ja = candidate_for(self.alice, name='Ann'), job_for(self.alice, title='Backend')
        self.cb, self.jb = candidate_for(self.bob, name='Ben'), job_for(self.bob, title='Bobs job')
        self.app = Application.objects.create(candidate=self.ca, job=self.ja, score=80, status='matched',
                                              reason='good fit', breakdown={'skills': 80}, evidence=[])
        self.bob_app = Application.objects.create(candidate=self.cb, job=self.jb, score=10, status='rejected')
        self.client.force_authenticate(self.alice)


class CandidateDetailTests(BoardBase):
    def test_returns_profile_applications_and_logs(self):
        AgentLog.objects.create(candidate=self.ca, from_agent='A', to_agent='B', message='m1')
        AgentLog.objects.create(candidate=self.cb, from_agent='A', to_agent='B', message='bob')
        d = self.client.get(f'/api/candidates/{self.ca.id}/detail/').data
        self.assertEqual(d['candidate']['name'], 'Ann')
        self.assertIn('resume_text', d['candidate'])
        self.assertNotIn('owner', d['candidate'])
        self.assertEqual([(a['job_title'], a['status'], a['reason']) for a in d['applications']],
                         [('Backend', 'matched', 'good fit')])
        self.assertEqual([l['message'] for l in d['logs']], ['m1'])

    def test_someone_elses_candidate_is_404(self):
        self.assertEqual(self.client.get(f'/api/candidates/{self.cb.id}/detail/').status_code, 404)

    def test_requires_login(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f'/api/candidates/{self.ca.id}/detail/').status_code, 401)


class ApplicationListTests(BoardBase):
    def test_lists_only_my_applications(self):
        rows = self.client.get('/api/applications/').data
        self.assertEqual([r['candidate_name'] for r in rows], ['Ann'])
        self.assertEqual(rows[0]['job_title'], 'Backend')

    def test_job_filter(self):
        j2 = job_for(self.alice, title='Other')
        c2 = candidate_for(self.alice, name='Cy')
        Application.objects.create(candidate=c2, job=j2, score=5, status='rejected')
        self.assertEqual(len(self.client.get(f'/api/applications/?job={self.ja.id}').data), 1)
        self.assertEqual(len(self.client.get('/api/applications/').data), 2)

    def test_filtering_by_someone_elses_job_returns_nothing(self):
        self.assertEqual(self.client.get(f'/api/applications/?job={self.jb.id}').data, [])

    def test_bad_job_value(self):
        self.assertEqual(self.client.get('/api/applications/?job=abc').status_code, 400)


class StatusMoveTests(BoardBase):
    def move(self, pk, status):
        return self.client.patch(f'/api/applications/{pk}/status/', {'status': status}, format='json')

    def test_move_updates_status_and_logs_it(self):
        r = self.move(self.app.id, 'shortlisted')
        self.assertEqual((r.status_code, r.data['status']), (200, 'shortlisted'))
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, 'shortlisted')
        self.assertTrue(AgentLog.objects.filter(candidate=self.ca, from_agent='Recruiter').exists())

    def test_invalid_status_is_rejected(self):
        self.assertEqual(self.move(self.app.id, 'hired').status_code, 400)
        self.assertEqual(self.move(self.app.id, '').status_code, 400)
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, 'matched')

    def test_cannot_move_someone_elses_application(self):
        self.assertEqual(self.move(self.bob_app.id, 'matched').status_code, 404)
        self.bob_app.refresh_from_db()
        self.assertEqual(self.bob_app.status, 'rejected')

    def test_template_demo_account_is_read_only(self):
        demo = User.objects.create_user('demo_recruiter', password='x-Pass-12345')
        c, j = candidate_for(demo), job_for(demo)
        a = Application.objects.create(candidate=c, job=j, score=1, status='pending')
        self.client.force_authenticate(demo)
        self.assertEqual(self.move(a.id, 'matched').status_code, 403)


class LogsCarryCandidateIdTests(BoardBase):
    def test_logs_include_candidate_id(self):
        AgentLog.objects.create(candidate=self.ca, from_agent='A', to_agent='B', message='x')
        self.assertEqual(self.client.get('/api/logs/').data[0]['candidate_id'], self.ca.id)
