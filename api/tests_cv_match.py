from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from .cv_matching import rank_jobs, split_skills
from .models import Job

User = get_user_model()

JOBS = [
    {'id': 1, 'title': 'Backend Developer', 'required_skills': 'Python, Django, SQL', 'min_experience': 2},
    {'id': 2, 'title': 'Frontend Developer', 'required_skills': 'React, JavaScript, CSS', 'min_experience': 1},
    {'id': 3, 'title': 'Java Developer', 'required_skills': 'Java, Spring', 'min_experience': 1},
]


class CvMatchingLogicTests(SimpleTestCase):
    def test_split_skills_normalises(self):
        self.assertEqual(split_skills(' Python ,Django,, SQL '), ['python', 'django', 'sql'])

    def test_best_fit_comes_first_with_verdicts(self):
        results = rank_jobs(JOBS, ['python', 'django', 'sql'], 3, '')
        self.assertEqual([r['title'] for r in results][0], 'Backend Developer')
        self.assertEqual(results[0]['verdict'], 'eligible')
        self.assertEqual(results[0]['missing_skills'], [])
        self.assertEqual({r['verdict'] for r in results[1:]}, {'not_a_match'})

    def test_experience_below_minimum_is_only_a_partial_match(self):
        results = rank_jobs(JOBS[:1], ['python', 'django', 'sql'], 1, '')
        self.assertEqual(results[0]['verdict'], 'partial')
        self.assertFalse(results[0]['experience_ok'])

    def test_skill_found_in_the_cv_text_counts_even_if_not_listed(self):
        results = rank_jobs(JOBS[:1], ['python'], 3, 'Built APIs with Django and wrote raw SQL queries.')
        self.assertEqual(results[0]['skill_percent'], 100)

    def test_whole_word_matching(self):
        results = rank_jobs(JOBS[2:], [], 3, 'I know javascript and spring cleaning.')
        self.assertIn('java', results[0]['missing_skills'])        # "javascript" must not count as java


class CvMatchEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('hr_cv', password='Str0ng!pass-93')
        self.other = User.objects.create_user('hr_other', password='Str0ng!pass-93')
        Job.objects.create(title='Backend', required_skills='Python, Django', min_experience=1,
                           description='d', owner=self.owner)
        Job.objects.create(title='Secret job of someone else', required_skills='Python', min_experience=0,
                           description='d', owner=self.other)
        self.client = APIClient()

    def test_requires_login(self):
        self.assertEqual(self.client.post('/api/match-cv/', {}, format='json').status_code, 401)

    def test_only_my_jobs_are_matched(self):
        self.client.force_authenticate(self.owner)
        r = self.client.post('/api/match-cv/', {'skills': ['python', 'django'], 'experience': 2}, format='json')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data['total_jobs'], 1)
        self.assertEqual(data['eligible_count'], 1)
        self.assertEqual(data['jobs'][0]['title'], 'Backend')

    def test_bad_input_is_rejected(self):
        self.client.force_authenticate(self.owner)
        r = self.client.post('/api/match-cv/', {'experience': 'lots'}, format='json')
        self.assertEqual(r.status_code, 400)