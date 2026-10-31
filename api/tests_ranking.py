from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from .agents import run_full_pipeline
from .models import AgentLog, Candidate, Job
from .ranking import build_leaderboard, competition_rank

User = get_user_model()


class RankingMathTests(SimpleTestCase):
    def test_competition_rank_with_ties(self):
        scores = [90, 80, 80, 60]
        self.assertEqual([competition_rank(s, scores) for s in scores], [1, 2, 2, 4])

    def test_leaderboard_puts_qualified_first_and_rejected_last_without_rank(self):
        rows = [
            {'name': 'D', 'score': 20, 'status': 'rejected'},
            {'name': 'B', 'score': 70, 'status': 'interview_scheduled'},
            {'name': 'A', 'score': 90, 'status': 'matched'},
        ]
        board = build_leaderboard(rows)
        self.assertEqual([r['name'] for r in board], ['A', 'B', 'D'])
        self.assertEqual([r['rank'] for r in board], [1, 2, None])


class JobWideRankingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('hr_one', password='Str0ng!pass-93')
        self.job = Job.objects.create(title='Backend', required_skills='python', min_experience=1,
                                      description='d', owner=self.owner)

    def candidate(self, name):
        return Candidate.objects.create(name=name, email=f'{name.lower()}@example.com', phone='1',
                                        skills='python', experience=3, resume_text='python', owner=self.owner)

    def run_for(self, candidate, score, match=True):
        card = {'overall': score, 'summary': 'ok', 'retrieval': 'semantic', 'evidence': [],
                'breakdown': {'skills': score, 'experience': 100, 'education': 50}}
        verdict = SimpleNamespace(is_match=match, match_percent=90, reason='fits')
        with mock.patch('api.agents.rag.retrieve_evidence', return_value={'mode': 'semantic'}), \
                mock.patch('api.agents.rag.score_candidate', return_value=card), \
                mock.patch('api.agents.generate_structured', return_value=verdict):
            return run_full_pipeline(candidate.pk, self.job.pk)

    def rank_message(self, candidate):
        return AgentLog.objects.get(candidate=candidate, from_agent='Candidate Ranking Agent').message

    def test_rank_is_relative_to_everyone_qualified_for_the_job(self):
        a, b, c = self.candidate('Asha'), self.candidate('Bala'), self.candidate('Chitra')
        self.run_for(a, 90)
        self.run_for(b, 70)
        self.run_for(c, 80)
        self.assertIn('Rank #1', self.rank_message(a))
        # the log is a snapshot from the moment of ranking: only Asha and Bala were qualified then
        self.assertIn('Rank #2', self.rank_message(b))
        self.assertIn('of 2', self.rank_message(b))
        self.assertIn('Rank #2', self.rank_message(c))
        self.assertIn('of 3', self.rank_message(c))

    def test_leaderboard_endpoint_orders_and_scopes_by_owner(self):
        a, b, d = self.candidate('Asha'), self.candidate('Bala'), self.candidate('Dev')
        self.run_for(b, 70)
        self.run_for(a, 90)
        self.run_for(d, 20)                        # rejected at screening
        client = APIClient()
        client.force_authenticate(self.owner)
        data = client.get(f'/api/jobs/{self.job.pk}/leaderboard/').json()
        self.assertEqual([c['name'] for c in data['candidates']], ['Asha', 'Bala', 'Dev'])
        self.assertEqual([c['rank'] for c in data['candidates']], [1, 2, None])

        other = User.objects.create_user('hr_two', password='Str0ng!pass-93')
        client.force_authenticate(other)
        self.assertEqual(client.get(f'/api/jobs/{self.job.pk}/leaderboard/').status_code, 404)

    def test_leaderboard_requires_login(self):
        self.assertEqual(APIClient().get(f'/api/jobs/{self.job.pk}/leaderboard/').status_code, 401)