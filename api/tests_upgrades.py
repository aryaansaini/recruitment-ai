"""Tests for the five upgrades: resume -> agents automation, live agent tracking, detailed evaluation,
interview management and the analytics dashboard. The AI is always mocked."""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from . import interviews
from .evaluation import build_evaluation, recommend
from .llm import LLMError, MatchResult, ScreeningResult
from .models import AgentLog, AgentStep, Application, Candidate, Interview, PipelineExecution
from .resume_parser import estimate_experience, extract_name
from .tests_multiuser import candidate_for, job_for


def ai(score=85, match=True, strengths=None, gaps=None):
    def fake(prompt, schema, **kw):
        if schema is ScreeningResult:
            return ScreeningResult(score=score, reason='Strong Django evidence',
                                   strengths=strengths or [], gaps=gaps or [])
        return MatchResult(is_match=match, match_percent=90 if match else 20, reason='Skills overlap')
    return fake


class Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user('alice', password='x-Pass-12345', email='alice@corp.test')
        self.bob = User.objects.create_user('bob', password='x-Pass-12345')
        self.client.force_authenticate(self.alice)
        self.cand = candidate_for(self.alice, name='Asha Rao', email='asha@example.com',
                                  skills='Python, Django, SQL', experience=4,
                                  resume_text='Built REST APIs with Python and Django. Used SQL daily.')
        self.job = job_for(self.alice, title='Backend Developer', required_skills='Python, Django, SQL, Docker',
                           min_experience=2)

    def patch_ai(self, **kw):
        p = mock.patch('api.agents.generate_structured', side_effect=ai(**kw))
        p.start()
        self.addCleanup(p.stop)

    def run_live(self, cand=None, job=None):
        r = self.client.post('/api/pipeline-runs/start/', {'candidate_id': (cand or self.cand).id,
                                                           'job_id': (job or self.job).id}, format='json')
        self.assertEqual(r.status_code, 202, r.data)
        return r.data


# ---------------------------------------------------------------------------
# 1. Resume -> agents automation (parser helpers + endpoint)
# ---------------------------------------------------------------------------
class ResumeAutomationTests(Base):
    def test_name_from_the_top_of_the_resume(self):
        self.assertEqual(extract_name('RAHUL SHARMA\nBackend Developer\nrahul@x.com', 'cv.pdf'), 'Rahul Sharma')

    def test_name_from_the_file_name_when_the_text_has_none(self):
        self.assertEqual(extract_name('Skills: Python\nExperience\n', 'aarav_mehta_resume_final.pdf'), 'Aarav Mehta')

    def test_no_name_found(self):
        self.assertEqual(extract_name('Skills: Python\nExperience\n', 'resume.pdf'), '')

    def test_experience_estimate(self):
        self.assertEqual(estimate_experience('5+ years of Django and 2 yrs of ML'), 5)
        self.assertEqual(estimate_experience('no numbers here'), 0)

    def test_parse_resume_returns_name_and_experience(self):
        text = b'Priya Nair\npriya@example.com\n+91 98765 43210\n\nSkills\nPython, Django\n\nExperience\n4 years at Acme\n'
        r = self.client.post('/api/parse-resume/', {'resume': SimpleUploadedFile('priya.txt', text)}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['name'], 'Priya Nair')
        self.assertEqual(r.data['experience_years'], 4)
        self.assertEqual(r.data['email'], 'priya@example.com')

    def test_match_cv_ranks_my_jobs_for_the_uploaded_resume(self):
        job_for(self.alice, title='Designer', required_skills='Figma, Sketch')
        r = self.client.post('/api/match-cv/', {'skills': ['Python', 'Django', 'SQL', 'Docker'], 'experience': 4,
                                                'resume_text': 'python django sql docker'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['jobs'][0]['title'], 'Backend Developer')
        self.assertEqual(r.data['jobs'][0]['verdict'], 'eligible')

    def test_clicking_a_job_runs_all_four_agents(self):
        """The page saves the candidate, then starts a live run: all four agents report in."""
        self.patch_ai(score=85)
        run = self.run_live()
        self.assertEqual(run['status'], 'completed')
        self.assertEqual([s['key'] for s in run['steps']], ['screening', 'matching', 'ranking', 'scheduling'])
        self.assertTrue(all(s['status'] == 'completed' for s in run['steps']))


# ---------------------------------------------------------------------------
# 2. Live agent execution tracking
# ---------------------------------------------------------------------------
class LiveTrackingTests(Base):
    def test_every_agent_reports_status_time_and_message(self):
        self.patch_ai(score=85)
        run = self.run_live()
        self.assertEqual(run['status'], 'completed')
        for step in run['steps']:
            self.assertEqual(step['status'], 'completed', step)
            self.assertIsNotNone(step['started_at'])
            self.assertIsNotNone(step['finished_at'])
            self.assertIsNotNone(step['duration_ms'])
            self.assertEqual(step['error'], '')
            self.assertTrue(step['message'])
        self.assertIn('Score 85', run['steps'][0]['message'])
        self.assertIsNotNone(run['duration_ms'])
        self.assertEqual(run['result']['status'], 'interview_scheduled')

    def test_rejected_candidate_skips_the_later_agents(self):
        self.patch_ai(score=20)
        steps = {s['key']: s['status'] for s in self.run_live()['steps']}
        self.assertEqual(steps, {'screening': 'completed', 'matching': 'skipped',
                                 'ranking': 'skipped', 'scheduling': 'skipped'})

    def test_no_match_skips_ranking_and_scheduling(self):
        self.patch_ai(score=80, match=False)
        steps = {s['key']: s['status'] for s in self.run_live()['steps']}
        self.assertEqual(steps['matching'], 'completed')
        self.assertEqual((steps['ranking'], steps['scheduling']), ('skipped', 'skipped'))

    def test_ai_failure_is_not_an_error_but_is_visible(self):
        with mock.patch('api.agents.generate_structured', side_effect=LLMError('quota')):
            run = self.run_live()
        self.assertEqual(run['status'], 'completed')
        self.assertIn('rule-based fallback', run['steps'][0]['message'])

    def test_a_crashing_agent_marks_the_step_and_the_run_as_failed(self):
        self.patch_ai(score=85)
        with mock.patch('api.agents.calendar_mcp', side_effect=RuntimeError('calendar down')):
            run = self.run_live()
        self.assertEqual(run['status'], 'failed')
        by_key = {s['key']: s for s in run['steps']}
        self.assertEqual(by_key['scheduling']['status'], 'failed')
        self.assertIn('calendar down', by_key['scheduling']['error'])
        self.assertIn('calendar down', run['error'])
        self.assertEqual(by_key['screening']['status'], 'completed')

    def test_get_run_returns_the_same_state(self):
        self.patch_ai(score=85)
        run = self.run_live()
        again = self.client.get(f"/api/pipeline-runs/{run['id']}/").data
        self.assertEqual(again['status'], 'completed')
        self.assertEqual(len(again['steps']), 4)

    def test_runs_of_other_recruiters_are_404(self):
        self.patch_ai(score=85)
        run = self.run_live()
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get(f"/api/pipeline-runs/{run['id']}/").status_code, 404)
        self.assertEqual(self.client.get('/api/pipeline-runs/').data, [])

    def test_cannot_start_a_run_for_someone_elses_candidate(self):
        other = candidate_for(self.bob, name='Ben')
        r = self.client.post('/api/pipeline-runs/start/', {'candidate_id': other.id, 'job_id': self.job.id}, format='json')
        self.assertEqual(r.status_code, 404)

    def test_requires_login(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post('/api/pipeline-runs/start/', {}, format='json').status_code, 401)

    def test_a_run_that_is_already_going_is_reused(self):
        execution = PipelineExecution.objects.create(owner=self.alice, candidate=self.cand, job=self.job,
                                                     status='running', started_at=timezone.now())
        r = self.client.post('/api/pipeline-runs/start/', {'candidate_id': self.cand.id, 'job_id': self.job.id}, format='json')
        self.assertEqual(r.data['id'], execution.id)
        self.assertTrue(r.data['already_running'])
        self.assertEqual(PipelineExecution.objects.count(), 1)

    def test_a_lost_run_is_failed_instead_of_spinning_forever(self):
        execution = PipelineExecution.objects.create(owner=self.alice, candidate=self.cand, job=self.job, status='running')
        PipelineExecution.objects.filter(pk=execution.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        data = self.client.get(f'/api/pipeline-runs/{execution.pk}/').data
        self.assertEqual(data['status'], 'failed')
        self.assertIn('did not finish', data['error'])

    def test_run_list_is_short_and_newest_first(self):
        self.patch_ai(score=85)
        first = self.run_live()
        job2 = job_for(self.alice, title='Second')
        second = self.run_live(job=job2)
        rows = self.client.get('/api/pipeline-runs/?limit=1').data
        self.assertEqual([r['id'] for r in rows], [second['id']])
        self.assertEqual(set(rows[0]['result']), {'score', 'status', 'rank'})
        self.assertNotEqual(first['id'], second['id'])

    def test_old_endpoint_still_works_and_is_untracked(self):
        self.patch_ai(score=85)
        r = self.client.post('/api/run-pipeline/', {'candidate_id': self.cand.id, 'job_id': self.job.id}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertIn('evaluation', r.data)
        self.assertEqual(PipelineExecution.objects.count(), 0)
        self.assertEqual(AgentStep.objects.count(), 0)

    def test_background_mode_starts_a_thread(self):
        self.patch_ai(score=85)
        with override_settings(PIPELINE_RUN_SYNC=False), mock.patch('api.pipeline_runs.threading.Thread') as thread:
            r = self.client.post('/api/pipeline-runs/start/', {'candidate_id': self.cand.id, 'job_id': self.job.id}, format='json')
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data['status'], 'queued')
        self.assertEqual([s['status'] for s in r.data['steps']], ['pending'] * 4)
        thread.return_value.start.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Detailed candidate evaluation
# ---------------------------------------------------------------------------
class EvaluationTests(Base):
    def evaluate(self, **kw):
        for k, v in kw.items():
            setattr(self.cand, k, v)
        return build_evaluation(self.cand, self.job)

    def test_skill_match_lists_matched_and_missing_skills(self):
        self.cand.score, self.cand.status = 80, 'interview_scheduled'
        ev = build_evaluation(self.cand, self.job)
        self.assertEqual(ev['skill_match']['matched'], ['Python', 'Django', 'SQL'])
        self.assertEqual(ev['skill_match']['missing'], ['Docker'])
        self.assertEqual(ev['skill_match']['percent'], 75)

    def test_a_skill_in_the_resume_text_counts_even_if_not_listed(self):
        self.cand.skills, self.cand.resume_text = 'Python', 'Worked with Docker and Django on SQL databases'
        ev = build_evaluation(self.cand, self.job)
        self.assertEqual(ev['skill_match']['missing'], [])

    def test_experience_match(self):
        ev = build_evaluation(self.cand, self.job)
        self.assertEqual(ev['experience_match'], {'candidate_years': 4, 'required_years': 2, 'percent': 100,
                                                  'meets': True, 'gap_years': 0, 'extra_years': 2})
        self.cand.experience = 1
        ev = build_evaluation(self.cand, self.job)
        self.assertFalse(ev['experience_match']['meets'])
        self.assertEqual(ev['experience_match']['gap_years'], 1)
        self.assertEqual(ev['experience_match']['percent'], 50)

    def test_weaknesses_name_the_missing_skills_and_experience_gap(self):
        self.cand.experience = 1
        ev = build_evaluation(self.cand, self.job)
        text = ' | '.join(ev['weaknesses'])
        self.assertIn('Docker', text)
        self.assertIn('below the 2-year minimum', text)

    def test_ai_strengths_and_gaps_come_first(self):
        self.cand.ai_strengths, self.cand.ai_gaps = ['Clean REST API design'], ['No cloud experience']
        ev = build_evaluation(self.cand, self.job)
        self.assertEqual(ev['strengths'][0], 'Clean REST API design')
        self.assertEqual(ev['weaknesses'][0], 'No cloud experience')

    def test_lists_are_capped(self):
        self.cand.ai_strengths = [f's{i}' for i in range(10)]
        self.assertLessEqual(len(build_evaluation(self.cand, self.job)['strengths']), 4)

    def test_recommendation_levels(self):
        self.assertEqual(recommend(90, 80, True, 'interview_scheduled')['decision'], 'strong_hire')
        self.assertEqual(recommend(70, 60, True, 'interview_scheduled')['decision'], 'hire')
        self.assertEqual(recommend(70, 60, False, 'interview_scheduled')['decision'], 'consider')
        self.assertEqual(recommend(55, 40, True, 'shortlisted')['decision'], 'consider')
        self.assertEqual(recommend(30, 20, True, 'shortlisted')['decision'], 'reject')
        self.assertEqual(recommend(95, 100, True, 'rejected')['decision'], 'reject')   # the pipeline said no

    def test_pipeline_stores_the_evaluation_on_the_application(self):
        self.patch_ai(score=88, strengths=['Strong Django'], gaps=['No Docker'])
        self.run_live()
        ev = Application.objects.get(candidate=self.cand, job=self.job).evaluation
        self.assertEqual(ev['overall_score'], 88)
        self.assertEqual(ev['skill_match']['missing'], ['Docker'])
        self.assertEqual(ev['recommendation']['decision'], 'strong_hire' if ev['skill_match']['percent'] >= 70 else 'hire')
        self.assertIn('Strong Django', ev['strengths'])
        self.assertFalse(ev['fallback_used'])

    def test_evaluation_exists_when_the_ai_is_down(self):
        with mock.patch('api.agents.generate_structured', side_effect=LLMError('down')):
            self.run_live()
        ev = Application.objects.get(candidate=self.cand, job=self.job).evaluation
        self.assertTrue(ev['fallback_used'])
        self.assertIn('missing', ev['skill_match'])

    def test_rejected_candidate_gets_a_reject_recommendation(self):
        self.patch_ai(score=20)
        self.run_live()
        ev = Application.objects.get(candidate=self.cand, job=self.job).evaluation
        self.assertEqual(ev['recommendation']['decision'], 'reject')

    def test_candidate_detail_includes_the_evaluation(self):
        self.patch_ai(score=88)
        self.run_live()
        d = self.client.get(f'/api/candidates/{self.cand.id}/detail/').data
        self.assertIn('recommendation', d['applications'][0]['evaluation'])


# ---------------------------------------------------------------------------
# 4. Interview management
# ---------------------------------------------------------------------------
def next_weekday_at(hour, days_ahead=5):
    day = timezone.now() + timedelta(days=days_ahead)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0)


class InterviewTests(Base):
    def setUp(self):
        super().setUp()
        self.app = Application.objects.create(candidate=self.cand, job=self.job, score=80, status='ranked')

    def schedule(self, when, app=None, **extra):
        return self.client.post(f'/api/applications/{(app or self.app).id}/interview/',
                                {'scheduled_at': when.isoformat(), **extra}, format='json')

    # --- agent -> interview record ---
    def test_the_pipeline_creates_an_interview_and_emails_the_candidate(self):
        self.patch_ai(score=88)
        run = self.run_live()
        self.assertTrue(run['result']['interview_id'])
        iv = Interview.objects.get(application__candidate=self.cand, application__job=self.job)
        self.assertEqual((iv.status, iv.owner), ('scheduled', self.alice))
        self.assertEqual(iv.application.status, 'interview_scheduled')
        recipients = sorted(sum((m.to for m in mail.outbox), []))
        self.assertEqual(recipients, ['alice@corp.test', 'asha@example.com'])
        self.assertIn('Interview scheduled', mail.outbox[0].subject)
        self.assertEqual(iv.history[0]['event'], 'scheduled')
        self.assertTrue(all(e['sent'] for e in iv.history[0]['email']))

    def test_a_taken_slot_is_moved_to_the_next_free_one(self):
        other = candidate_for(self.alice, name='Ben', email='ben@example.com')
        other_app = Application.objects.create(candidate=other, job=self.job, score=70, status='interview_scheduled')
        taken = timezone.now() + timedelta(days=3)
        Interview.objects.create(owner=self.alice, application=other_app, candidate=other, job=self.job, scheduled_at=taken)
        self.patch_ai(score=88)
        with mock.patch('api.agents.calendar_mcp', return_value={
                'candidate': 'Asha Rao', 'slot': taken.strftime('%Y-%m-%d %H:%M'), 'datetime': taken,
                'platform': 'Google Meet', 'link': 'meet.google.com/x'}):
            self.run_live()
        iv = Interview.objects.get(application__candidate=self.cand)
        self.assertNotEqual(iv.scheduled_at, taken)
        self.assertGreater(iv.scheduled_at, taken)
        self.assertTrue(AgentLog.objects.filter(candidate=self.cand, message__contains='already booked').exists())

    def test_rerun_that_rejects_cancels_the_old_interview(self):
        self.patch_ai(score=88)
        self.run_live()
        mail.outbox.clear()
        self.patch_ai(score=10)
        self.run_live()
        iv = Interview.objects.get(application__candidate=self.cand)
        self.assertEqual(iv.status, 'cancelled')
        self.assertEqual(Application.objects.get(candidate=self.cand, job=self.job).status, 'rejected')
        self.assertTrue(any('cancelled' in m.subject for m in mail.outbox))

    def test_rerun_that_qualifies_again_keeps_a_single_interview(self):
        self.patch_ai(score=88)
        self.run_live()
        self.run_live()
        self.assertEqual(Interview.objects.filter(application__candidate=self.cand).count(), 1)

    # --- free slots ---
    def test_free_slots_are_future_weekday_working_hours(self):
        slots = self.client.get('/api/interviews/slots/?days=14').data['slots']
        self.assertTrue(slots)
        now = timezone.now()
        for s in slots:
            start = datetime.fromisoformat(s['start'])
            self.assertGreater(start, now)
            self.assertLess(start.weekday(), 5)
            self.assertIn(start.hour, range(10, 17))

    def test_booked_slots_are_not_offered(self):
        when = next_weekday_at(10)
        self.schedule(when)
        starts = [s['start'] for s in self.client.get(f'/api/interviews/slots/?days=14&start={when.date()}').data['slots']]
        self.assertNotIn(when.isoformat(), starts)

    def test_the_interview_being_moved_does_not_block_its_own_slot(self):
        when = next_weekday_at(10)
        self.schedule(when)
        starts = [s['start'] for s in self.client.get(
            f'/api/interviews/slots/?days=14&start={when.date()}&application={self.app.id}').data['slots']]
        self.assertIn(when.isoformat(), starts)

    def test_slots_of_other_recruiters_do_not_block_mine(self):
        when = next_weekday_at(10)
        other = candidate_for(self.bob, name='Ben')
        job = job_for(self.bob)
        app = Application.objects.create(candidate=other, job=job, status='ranked')
        Interview.objects.create(owner=self.bob, application=app, candidate=other, job=job, scheduled_at=when)
        starts = [s['start'] for s in self.client.get(f'/api/interviews/slots/?days=14&start={when.date()}').data['slots']]
        self.assertIn(when.isoformat(), starts)

    def test_bad_slot_parameters(self):
        self.assertEqual(self.client.get('/api/interviews/slots/?days=x').status_code, 400)
        self.assertEqual(self.client.get('/api/interviews/slots/?start=tomorrow').status_code, 400)

    # --- schedule ---
    def test_schedule_sets_statuses_and_sends_email(self):
        when = next_weekday_at(11)
        r = self.schedule(when, notes='Bring a laptop')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['status'], 'scheduled')
        self.app.refresh_from_db()
        self.cand.refresh_from_db()
        self.assertEqual((self.app.status, self.app.interview_date), ('interview_scheduled', when))
        self.assertEqual((self.cand.status, self.cand.interview_date), ('interview_scheduled', when))
        self.assertEqual(len(mail.outbox), 2)

    def test_cannot_schedule_in_the_past(self):
        r = self.schedule(timezone.now() - timedelta(days=1))
        self.assertEqual(r.status_code, 400)
        self.assertIn('future', r.data['detail'])

    def test_overlapping_interview_is_refused(self):
        when = next_weekday_at(10)
        self.schedule(when)
        other = candidate_for(self.alice, name='Ben', email='ben@example.com')
        app2 = Application.objects.create(candidate=other, job=self.job, status='ranked')
        r = self.schedule(when + timedelta(minutes=30), app=app2)
        self.assertEqual(r.status_code, 409)
        self.assertIn('overlaps', r.data['detail'])
        self.assertEqual(self.schedule(when + timedelta(minutes=60), app=app2).status_code, 201)   # back to back is fine

    def test_second_interview_for_the_same_application_is_refused(self):
        self.schedule(next_weekday_at(10))
        self.assertEqual(self.schedule(next_weekday_at(12)).status_code, 409)

    def test_someone_elses_application_is_404(self):
        other = candidate_for(self.bob, name='Ben')
        app = Application.objects.create(candidate=other, job=job_for(self.bob), status='ranked')
        self.assertEqual(self.schedule(next_weekday_at(10), app=app).status_code, 404)

    # --- reschedule ---
    def test_reschedule_moves_the_interview_and_emails(self):
        first, second = next_weekday_at(10), next_weekday_at(14)
        iv = self.schedule(first).data
        mail.outbox.clear()
        r = self.client.patch(f"/api/interviews/{iv['id']}/", {'scheduled_at': second.isoformat(), 'reason': 'Panel clash'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['status'], 'rescheduled')
        self.app.refresh_from_db()
        self.assertEqual(self.app.interview_date, second)
        self.assertIn('rescheduled', mail.outbox[0].subject)
        self.assertIn('Panel clash', mail.outbox[0].body)
        self.assertEqual([h['event'] for h in r.data['history']], ['scheduled', 'rescheduled'])

    def test_reschedule_to_a_taken_time_is_a_conflict(self):
        when = next_weekday_at(10)
        self.schedule(when)
        other = candidate_for(self.alice, name='Ben', email='ben@example.com')
        app2 = Application.objects.create(candidate=other, job=self.job, status='ranked')
        iv2 = self.schedule(next_weekday_at(14), app=app2).data
        r = self.client.patch(f"/api/interviews/{iv2['id']}/", {'scheduled_at': when.isoformat()}, format='json')
        self.assertEqual(r.status_code, 409)

    def test_reschedule_to_the_past_is_refused(self):
        iv = self.schedule(next_weekday_at(10)).data
        r = self.client.patch(f"/api/interviews/{iv['id']}/", {'scheduled_at': (timezone.now() - timedelta(hours=1)).isoformat()}, format='json')
        self.assertEqual(r.status_code, 400)

    # --- cancel / complete ---
    def test_cancel_frees_the_slot_resets_statuses_and_emails(self):
        when = next_weekday_at(10)
        iv = self.schedule(when).data
        mail.outbox.clear()
        r = self.client.post(f"/api/interviews/{iv['id']}/cancel/", {'reason': 'Role on hold'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'cancelled')
        self.app.refresh_from_db()
        self.cand.refresh_from_db()
        self.assertEqual((self.app.status, self.app.interview_date), ('ranked', None))
        self.assertEqual((self.cand.status, self.cand.interview_date), ('ranked', None))
        self.assertIn('cancelled', mail.outbox[0].subject)
        self.assertIn('Role on hold', mail.outbox[0].body)
        starts = [s['start'] for s in self.client.get(f'/api/interviews/slots/?days=14&start={when.date()}').data['slots']]
        self.assertIn(when.isoformat(), starts)

    def test_cancelled_interview_cannot_be_cancelled_or_rescheduled(self):
        iv = self.schedule(next_weekday_at(10)).data
        self.client.post(f"/api/interviews/{iv['id']}/cancel/", {}, format='json')
        self.assertEqual(self.client.post(f"/api/interviews/{iv['id']}/cancel/", {}, format='json').status_code, 409)
        r = self.client.patch(f"/api/interviews/{iv['id']}/", {'scheduled_at': next_weekday_at(12).isoformat()}, format='json')
        self.assertEqual(r.status_code, 409)

    def test_a_cancelled_application_can_be_scheduled_again(self):
        iv = self.schedule(next_weekday_at(10)).data
        self.client.post(f"/api/interviews/{iv['id']}/cancel/", {}, format='json')
        r = self.schedule(next_weekday_at(12))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Interview.objects.filter(application=self.app).count(), 1)
        self.assertEqual(r.data['status'], 'scheduled')

    def test_complete(self):
        iv = self.schedule(next_weekday_at(10)).data
        r = self.client.post(f"/api/interviews/{iv['id']}/complete/", {'notes': 'Great fit'}, format='json')
        self.assertEqual(r.data['status'], 'completed')
        self.assertEqual(r.data['notes'], 'Great fit')
        self.assertEqual(self.client.post(f"/api/interviews/{iv['id']}/cancel/", {}, format='json').status_code, 409)
        self.assertEqual(self.client.post(f"/api/interviews/{iv['id']}/complete/", {}, format='json').status_code, 409)

    def test_cancelling_one_of_two_interviews_keeps_the_candidate_scheduled(self):
        job2 = job_for(self.alice, title='Second job')
        app2 = Application.objects.create(candidate=self.cand, job=job2, status='ranked')
        a = self.schedule(next_weekday_at(10)).data
        self.schedule(next_weekday_at(12), app=app2)
        self.client.post(f"/api/interviews/{a['id']}/cancel/", {}, format='json')
        self.cand.refresh_from_db()
        self.assertEqual(self.cand.status, 'interview_scheduled')

    # --- list / isolation ---
    def test_list_filters_by_status_and_date_and_is_private(self):
        a = self.schedule(next_weekday_at(10)).data
        other = candidate_for(self.alice, name='Ben', email='ben@example.com')
        app2 = Application.objects.create(candidate=other, job=self.job, status='ranked')
        b = self.schedule(next_weekday_at(12, days_ahead=9), app=app2).data
        self.client.post(f"/api/interviews/{b['id']}/cancel/", {}, format='json')
        self.assertEqual({i['id'] for i in self.client.get('/api/interviews/').data}, {a['id'], b['id']})
        self.assertEqual([i['id'] for i in self.client.get('/api/interviews/?status=cancelled').data], [b['id']])
        day = a['scheduled_at'].date()
        self.assertEqual([i['id'] for i in self.client.get(f'/api/interviews/?from={day}&to={day}').data], [a['id']])
        self.assertEqual(self.client.get('/api/interviews/?status=nope').status_code, 400)
        self.assertEqual(self.client.get('/api/interviews/?from=soon').status_code, 400)
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get('/api/interviews/').data, [])
        self.assertEqual(self.client.post(f"/api/interviews/{a['id']}/cancel/", {}, format='json').status_code, 404)
        self.assertEqual(self.client.patch(f"/api/interviews/{a['id']}/", {'scheduled_at': next_weekday_at(15).isoformat()}, format='json').status_code, 404)

    def test_requires_login(self):
        self.client.force_authenticate(None)
        for url in ('/api/interviews/', '/api/interviews/slots/', '/api/analytics/'):
            self.assertEqual(self.client.get(url).status_code, 401, url)

    # --- e-mail behaviour ---
    def test_a_mail_failure_does_not_undo_the_interview(self):
        with mock.patch('api.interviews.send_mail', side_effect=OSError('smtp down')):
            r = self.schedule(next_weekday_at(10))
        self.assertEqual(r.status_code, 201)
        email = r.data['history'][0]['email']
        self.assertTrue(email and not any(e['sent'] for e in email))
        self.assertIn('smtp down', email[0]['error'])

    def test_emails_can_be_switched_off(self):
        with override_settings(INTERVIEW_EMAILS_ENABLED=False):
            self.schedule(next_weekday_at(10))
        self.assertEqual(mail.outbox, [])

    def test_demo_accounts_never_send_email(self):
        demo = User.objects.create_user('demo_session_abc', password='x-Pass-12345')
        c = candidate_for(demo, name='Dee', email='dee@example.com')
        app = Application.objects.create(candidate=c, job=job_for(demo), status='ranked')
        self.client.force_authenticate(demo)
        r = self.schedule(next_weekday_at(10), app=app)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(mail.outbox, [])

    def test_daily_email_limit(self):
        with override_settings(INTERVIEW_EMAILS_PER_DAY=1):
            self.schedule(next_weekday_at(10))          # uses the one allowed notification
            other = candidate_for(self.alice, name='Ben', email='ben@example.com')
            app2 = Application.objects.create(candidate=other, job=self.job, status='ranked')
            n = len(mail.outbox)
            self.schedule(next_weekday_at(12), app=app2)
        self.assertEqual(len(mail.outbox), n)

    def test_template_demo_workspace_is_read_only(self):
        template = User.objects.create_user('demo_recruiter', password='x-Pass-12345')
        self.client.force_authenticate(template)
        self.assertEqual(self.client.post('/api/applications/1/interview/', {'scheduled_at': next_weekday_at(10).isoformat()}, format='json').status_code, 403)

    def test_service_layer_snaps_nothing_when_the_slot_is_free(self):
        when = next_weekday_at(10)
        self.app.status, self.app.interview_date = 'interview_scheduled', when
        self.app.save()
        iv = interviews.sync_from_pipeline(self.app, {'platform': 'Zoom', 'link': 'zoom.test/abc'})
        self.assertEqual((iv.scheduled_at, iv.platform, iv.link), (when, 'Zoom', 'zoom.test/abc'))


# ---------------------------------------------------------------------------
# 5. Analytics dashboard
# ---------------------------------------------------------------------------
class AnalyticsTests(Base):
    def setUp(self):
        super().setUp()
        self.job2 = job_for(self.alice, title='Data Analyst', required_skills='SQL, Excel')
        c2 = candidate_for(self.alice, name='Ben', email='ben@example.com')
        c3 = candidate_for(self.alice, name='Cara', email='cara@example.com')
        self.c4 = candidate_for(self.alice, name='Dev', email='dev@example.com')      # never screened
        ev = {'skill_match': {'missing': ['Docker', 'Kubernetes']}}
        Application.objects.create(candidate=self.cand, job=self.job, score=90, status='interview_scheduled', evaluation=ev)
        Application.objects.create(candidate=c2, job=self.job, score=70, status='matched', evaluation={'skill_match': {'missing': ['Docker']}})
        Application.objects.create(candidate=c3, job=self.job, score=20, status='rejected')
        Application.objects.create(candidate=self.cand, job=self.job2, score=55, status='shortlisted', fallback_used=True)
        app = Application.objects.get(candidate=self.cand, job=self.job)
        Interview.objects.create(owner=self.alice, application=app, candidate=self.cand, job=self.job,
                                 scheduled_at=timezone.now() + timedelta(days=2))
        # someone else's data must never leak in
        bob_c, bob_j = candidate_for(self.bob, name='Ben2'), job_for(self.bob, title='Bob job')
        Application.objects.create(candidate=bob_c, job=bob_j, score=99, status='interview_scheduled')

    def get(self, query=''):
        r = self.client.get('/api/analytics/' + query)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_totals(self):
        t = self.get()['totals']
        self.assertEqual(t['candidates'], 4)
        self.assertEqual(t['jobs'], 2)
        self.assertEqual(t['applications'], 4)
        self.assertEqual(t['shortlisted'], 3)          # interview_scheduled + matched + shortlisted
        self.assertEqual(t['rejected'], 1)
        self.assertEqual(t['interviews_scheduled'], 1)
        self.assertEqual(t['not_yet_screened'], 1)
        self.assertEqual(t['fallback_scores'], 1)

    def test_funnel_and_conversion(self):
        d = self.get()
        self.assertEqual([(f['key'], f['count']) for f in d['funnel']],
                         [('applied', 4), ('shortlisted', 3), ('matched', 2), ('interview', 1)])
        self.assertEqual(d['funnel'][1]['conversion_from_previous'], 75.0)
        self.assertEqual(d['funnel'][3]['percent_of_applied'], 25.0)
        self.assertEqual(d['conversion']['hiring_conversion'], 25.0)
        self.assertEqual(d['conversion']['rejection_rate'], 25.0)

    def test_job_wise_matching(self):
        rows = {r['title']: r for r in self.get()['jobs']}
        backend = rows['Backend Developer']
        self.assertEqual((backend['applications'], backend['shortlisted'], backend['rejected'], backend['interviews']), (3, 2, 1, 1))
        self.assertEqual(backend['average_score'], 60.0)
        self.assertEqual(backend['top_score'], 90.0)
        self.assertEqual(backend['match_rate'], 66.7)
        self.assertEqual(rows['Data Analyst']['applications'], 1)

    def test_job_filter(self):
        d = self.get(f'?job={self.job2.id}')
        self.assertEqual(d['totals']['applications'], 1)
        self.assertEqual([r['title'] for r in d['jobs']], ['Data Analyst'])
        self.assertEqual(self.client.get('/api/analytics/?job=abc').status_code, 400)

    def test_score_distribution_missing_skills_and_activity(self):
        d = self.get()
        self.assertEqual({b['range']: b['count'] for b in d['score_distribution']},
                         {'0-39': 1, '40-59': 1, '60-79': 1, '80-100': 1})
        self.assertEqual(d['top_missing_skills'][0], {'skill': 'docker', 'count': 2})
        self.assertEqual(len(d['evaluations_by_day']), 14)
        self.assertEqual(d['evaluations_by_day'][-1]['count'], 4)

    def test_upcoming_interviews_and_pipeline_health(self):
        d = self.get()
        self.assertEqual([u['candidate_name'] for u in d['upcoming_interviews']], ['Asha Rao'])
        self.assertEqual(d['pipeline_health']['runs_30d'], 0)
        self.patch_ai(score=88)
        self.run_live(cand=self.c4)
        health = self.get()['pipeline_health']
        self.assertEqual((health['runs_30d'], health['completed'], health['failed']), (1, 1, 0))
        self.assertIn('screening', health['agent_average_ms'])

    def test_empty_account(self):
        self.client.force_authenticate(User.objects.create_user('empty', password='x-Pass-12345'))
        d = self.get()
        self.assertEqual(d['totals']['applications'], 0)
        self.assertEqual(d['conversion']['hiring_conversion'], 0.0)
        self.assertEqual(d['jobs'], [])
        self.assertEqual(d['funnel'][0]['count'], 0)
