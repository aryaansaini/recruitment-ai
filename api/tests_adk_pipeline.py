
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .adk_pipeline import PipelineRun, build_pipeline, run_adk_pipeline
from .agents import run_full_pipeline
from .models import AgentLog, Application, Candidate, Job

User = get_user_model()


class AdkPipelineTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('hr_adk', password='Str0ng!pass-93')
        self.job = Job.objects.create(title='Backend', required_skills='python', min_experience=1,
                                      description='d', owner=self.owner)

    def candidate(self, name):
        return Candidate.objects.create(name=name, email=f'{name.lower()}@example.com', phone='1',
                                        skills='python', experience=3, resume_text='python', owner=self.owner)

    def mocked(self, score, match=True):
        """No Gemini: the scoring and matching calls are replaced by fixed answers."""
        card = {'overall': score, 'summary': 'ok', 'retrieval': 'semantic', 'evidence': [],
                'breakdown': {'skills': score, 'experience': 100, 'education': 50}}
        verdict = SimpleNamespace(is_match=match, match_percent=90, reason='fits')
        stack = ExitStack()
        stack.enter_context(mock.patch('api.agents.rag.retrieve_evidence', return_value={'mode': 'semantic'}))
        stack.enter_context(mock.patch('api.agents.rag.score_candidate', return_value=card))
        stack.enter_context(mock.patch('api.agents.generate_structured', return_value=verdict))
        return stack

    def test_pipeline_is_a_sequential_agent_of_four_named_agents(self):
        from google.adk.agents import SequentialAgent
        pipeline = build_pipeline(PipelineRun(candidate=None, job=None))
        self.assertIsInstance(pipeline, SequentialAgent)
        self.assertEqual([a.name for a in pipeline.sub_agents],
                         ['screening_agent', 'matching_agent', 'ranking_agent', 'scheduling_agent'])

    def test_qualified_candidate_goes_through_all_four_agents(self):
        c = self.candidate('Asha')
        with self.mocked(90):
            outcome = run_adk_pipeline(c, self.job)
        self.assertTrue(outcome.qualified)
        self.assertIsNotNone(outcome.slot)
        for key in ('screening_done', 'matching_done', 'ranking_done', 'scheduling_done'):
            self.assertTrue(outcome.session_state.get(key), key)

    def test_rejected_candidate_skips_ranking_and_scheduling(self):
        c = self.candidate('Dev')
        with self.mocked(20):
            outcome = run_adk_pipeline(c, self.job)
        self.assertFalse(outcome.qualified)
        self.assertIsNone(outcome.slot)
        self.assertTrue(outcome.session_state.get('ranking_skipped'))
        self.assertTrue(outcome.session_state.get('scheduling_skipped'))
        self.assertEqual(AgentLog.objects.filter(candidate=c, from_agent='Candidate Ranking Agent').count(), 0)

    def test_adk_and_plain_engines_give_the_same_result(self):
        results = []
        for use_adk, name in ((True, 'ViaAdk'), (False, 'ViaPlain')):
            c = self.candidate(name)
            with override_settings(PIPELINE_USE_ADK=use_adk), self.mocked(85):
                candidate, slot = run_full_pipeline(c.pk, self.job.pk)
            application = Application.objects.get(candidate=c, job=self.job)
            results.append((candidate.status, candidate.score, slot is not None, application.status))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 'interview_scheduled')

    def test_run_pipeline_endpoint_returns_the_rank(self):
        c = self.candidate('Asha')
        client = APIClient()
        client.force_authenticate(self.owner)
        with self.mocked(90):
            r = client.post('/api/run-pipeline/', {'candidate_id': c.pk, 'job_id': self.job.pk}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['rank'], 1)