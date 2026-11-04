"""The LlmAgent pipeline. Gemini is replaced by a scripted model; ADK, the tools and the database are real."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

from . import adk_pipeline_llm as llm_pipeline
from .adk_pipeline import PipelineRun, build_pipeline
from .agents import run_full_pipeline
from .models import AgentLog, Application, Candidate, Job
from .services import rebuild_resume_chunks

User = get_user_model()

SCREENING, MATCHING, RANKING, SCHEDULING = (llm_pipeline.LABELS[s] for s in llm_pipeline.ORDER)

DEFAULT_SCRIPTS = {
    SCREENING: [('get_job_requirements', {}), ('search_resume_chunks', {'query': 'python django'}),
                ('search_resume_chunks', {'query': 'REST APIs', 'top_k': 3}), ('score_and_save_candidate', {})],
    MATCHING: [('get_job_requirements', {}), ('get_candidate_profile', {}),
               ('record_match_decision', {'is_match': True, 'match_percent': 90, 'reason': 'Covers the skills.'})],
    RANKING: [('get_job_leaderboard', {}), ('rank_candidate', {})],
    SCHEDULING: [('propose_interview_slot', {})],
}


class ScriptedLlm(BaseLlm):
    """Plays a fixed list of tool calls per agent, then answers with one sentence."""
    model: str = 'scripted'
    scripts: dict = Field(default_factory=lambda: dict(DEFAULT_SCRIPTS))
    fail_for: list = Field(default_factory=list)
    calls: list = Field(default_factory=list)          # which agent asked, one entry per model call

    @classmethod
    def supported_models(cls):
        return ['scripted']

    async def generate_content_async(self, llm_request, stream=False):
        instruction = str(llm_request.config.system_instruction)
        agent = next(label for label in DEFAULT_SCRIPTS if label in instruction)
        self.calls.append(agent)
        if agent in self.fail_for:
            raise RuntimeError('quota exhausted')
        answered = sum(1 for c in llm_request.contents for p in c.parts if p.function_response)
        script = self.scripts.get(agent, [])
        if answered < len(script):
            name, args = script[answered]
            part = types.Part(function_call=types.FunctionCall(name=name, args=args))
        else:
            part = types.Part(text='Done.')
        yield LlmResponse(content=types.Content(role='model', parts=[part]))


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user('hr_llm', password='Str0ng!pass-93')
        self.job = Job.objects.create(title='Backend', required_skills='python, django', min_experience=1,
                                      description='Build APIs', owner=self.owner)

    def candidate(self, name='Asha', resume='Skills\npython django\nExperience\nBuilt REST APIs with Django.'):
        c = Candidate.objects.create(name=name, email=f'{name.lower()}@example.com', phone='1', skills='python, django',
                                     experience=3, resume_text=resume, owner=self.owner)
        rebuild_resume_chunks(c)
        return c

    def mocked(self, score=90):
        """No Gemini behind the tools: scoring is a fixed card, and the plain matching call must not be needed."""
        self.card = {'overall': score, 'summary': 'ok', 'retrieval': 'semantic', 'evidence': [],
                     'breakdown': {'skills': score, 'experience': 100, 'education': 50}}
        self.verdict = SimpleNamespace(is_match=True, match_percent=70, reason='built-in match')
        stack = ExitStack()
        stack.enter_context(mock.patch('api.agents.rag.retrieve_evidence',
                                       return_value={'mode': 'semantic', 'chunks': []}))
        self.score_mock = stack.enter_context(mock.patch('api.agents.rag.score_candidate', return_value=self.card))
        self.match_mock = stack.enter_context(mock.patch('api.agents.generate_structured', return_value=self.verdict))
        self.addCleanup(stack.close)
        return stack

    def logs(self, c):
        return [(l.from_agent, l.to_agent, l.message) for l in AgentLog.objects.filter(candidate=c).order_by('id')]

    def edges(self, c):
        return [(a, b) for a, b, _m in self.logs(c)]


class LlmAgentsTests(Base):
    def test_pipeline_is_four_llm_agents_with_tools(self):
        pipeline = llm_pipeline.build_llm_pipeline(PipelineRun(candidate=None, job=None), model=ScriptedLlm())
        self.assertIsInstance(pipeline, SequentialAgent)
        self.assertTrue(all(isinstance(a, LlmAgent) for a in pipeline.sub_agents))
        tools = {a.name: [t.__name__ for t in a.tools] for a in pipeline.sub_agents}
        self.assertEqual(tools['screening_agent'], ['get_job_requirements', 'search_resume_chunks', 'score_and_save_candidate'])
        self.assertEqual(tools['matching_agent'], ['get_job_requirements', 'get_candidate_profile', 'record_match_decision'])
        self.assertEqual(tools['ranking_agent'], ['get_job_leaderboard', 'rank_candidate'])
        self.assertEqual(tools['scheduling_agent'], ['propose_interview_slot'])

    def test_no_tool_accepts_a_candidate_or_job_id(self):
        run = PipelineRun(candidate=None, job=None)
        for tool_list in llm_pipeline.build_tools(run).values():
            for tool in tool_list:
                self.assertFalse({'candidate_id', 'job_id', 'id'} & set(tool.__code__.co_varnames[:tool.__code__.co_argcount]))

    def test_every_agent_has_the_callbacks(self):
        pipeline = llm_pipeline.build_llm_pipeline(PipelineRun(candidate=None, job=None), model=ScriptedLlm())
        for agent in pipeline.sub_agents:
            for name in ('before_agent_callback', 'after_agent_callback', 'before_model_callback',
                         'on_model_error_callback', 'after_tool_callback', 'on_tool_error_callback'):
                self.assertTrue(getattr(agent, name), f'{agent.name}.{name}')

    def test_real_gemini_model_object_can_be_built(self):
        self.assertEqual(llm_pipeline.make_model().model, 'gemini-2.5-flash')


class HappyPathTests(Base):
    def test_qualified_candidate_is_handled_by_the_agents_through_their_tools(self):
        c = self.candidate()
        self.mocked(90)
        model = ScriptedLlm()
        run = llm_pipeline.run_llm_pipeline(c, self.job, model=model)

        c.refresh_from_db()
        self.assertEqual((c.status, c.score), ('interview_scheduled', 90))
        self.assertTrue(run.qualified)
        self.assertIsNotNone(run.slot)
        self.assertEqual(run.done, {'screening', 'matching', 'ranking', 'scheduling'})
        self.assertEqual(run.results['ranking']['rank'], 1)
        # the agents decided the match themselves: the plain Gemini matching call was never made
        self.match_mock.assert_not_called()
        self.score_mock.assert_called_once()
        # model calls: one per tool call plus the final sentence of each agent
        self.assertEqual({a: model.calls.count(a) for a in set(model.calls)},
                         {SCREENING: 5, MATCHING: 4, RANKING: 3, SCHEDULING: 2})
        for key in ('screening_done', 'matching_done', 'qualified', 'ranking_done', 'scheduling_done'):
            self.assertTrue(run.session_state.get(key), key)

    def test_activity_log_is_written_by_callbacks_in_order_and_once(self):
        c = self.candidate()
        self.mocked(90)
        llm_pipeline.run_llm_pipeline(c, self.job, model=ScriptedLlm())
        self.assertEqual(self.edges(c), [
            (SCREENING, 'Tool: search_resume_chunks'),
            (SCREENING, 'Tool: search_resume_chunks'),
            (SCREENING, MATCHING),
            (MATCHING, RANKING),
            (RANKING, SCHEDULING),
            (SCHEDULING, 'Recruiter Dashboard'),
        ])
        messages = [m for _a, _b, m in self.logs(c)]
        self.assertIn('Searched the resume for "python django"', messages[0])
        self.assertIn('AI Score: 90', messages[2])                 # same hand-off text as before
        self.assertIn('Matched (90%). Covers the skills.', messages[3])
        self.assertIn('Rank #1', messages[4])

    def test_tools_only_queue_the_log_the_callback_writes_it(self):
        c = self.candidate()
        self.mocked(90)
        run = PipelineRun(candidate=c, job=self.job)
        llm_pipeline._score(run)
        self.assertEqual(AgentLog.objects.count(), 0)              # nothing written by the tool itself
        self.assertEqual(len(run.pending_logs), 1)
        run.write_pending_logs()                                   # what the after_agent callback does
        self.assertEqual(AgentLog.objects.count(), 1)
        self.assertEqual(run.pending_logs, [])

    def test_searched_resume_parts_are_added_to_the_scoring_evidence(self):
        c = self.candidate()
        self.mocked(90)
        llm_pipeline.run_llm_pipeline(c, self.job, model=ScriptedLlm())
        evidence = self.score_mock.call_args.kwargs['evidence']
        self.assertTrue(evidence['chunks'])
        self.assertTrue(all(ch['chunk_id'] for ch in evidence['chunks']))
        ids = [ch['chunk_id'] for ch in evidence['chunks']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_rejected_candidate_stops_after_screening_without_more_model_calls(self):
        c = self.candidate('Dev')
        self.mocked(20)
        model = ScriptedLlm()
        run = llm_pipeline.run_llm_pipeline(c, self.job, model=model)
        c.refresh_from_db()
        self.assertEqual(c.status, 'rejected')
        self.assertFalse(run.qualified)
        self.assertIsNone(run.slot)
        self.assertEqual(set(model.calls), {SCREENING})            # matching, ranking, scheduling: no model call
        self.assertTrue(run.session_state.get('ranking_skipped'))
        self.assertTrue(run.session_state.get('scheduling_skipped'))
        self.assertTrue(run.session_state.get('matching_done'))
        self.assertEqual(AgentLog.objects.filter(candidate=c, from_agent=RANKING).count(), 0)

    def test_a_negative_decision_by_the_matching_agent_rejects(self):
        c = self.candidate()
        self.mocked(90)
        scripts = dict(DEFAULT_SCRIPTS)
        scripts[MATCHING] = [('record_match_decision', {'is_match': False, 'match_percent': 20, 'reason': 'Missing Django.'})]
        model = ScriptedLlm(scripts=scripts)
        run = llm_pipeline.run_llm_pipeline(c, self.job, model=model)
        c.refresh_from_db()
        self.assertEqual(c.status, 'rejected')
        self.assertFalse(run.qualified)
        self.assertNotIn(RANKING, model.calls)

    def test_calling_a_tool_twice_changes_nothing_the_second_time(self):
        c = self.candidate()
        self.mocked(90)
        scripts = dict(DEFAULT_SCRIPTS)
        scripts[SCREENING] = [('score_and_save_candidate', {}), ('score_and_save_candidate', {})]
        llm_pipeline.run_llm_pipeline(c, self.job, model=ScriptedLlm(scripts=scripts))
        self.score_mock.assert_called_once()
        self.assertEqual(AgentLog.objects.filter(candidate=c, from_agent=SCREENING, to_agent=MATCHING).count(), 1)


class ToolSafetyTests(Base):
    def test_ids_in_the_model_arguments_are_ignored(self):
        mine, other = self.candidate('Asha'), self.candidate('Other')
        self.mocked(90)
        scripts = dict(DEFAULT_SCRIPTS)
        scripts[RANKING] = [('rank_candidate', {'candidate_id': other.pk, 'job_id': 999})]
        llm_pipeline.run_llm_pipeline(mine, self.job, model=ScriptedLlm(scripts=scripts))
        other.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual(other.status, 'pending')
        self.assertEqual(AgentLog.objects.filter(candidate=other).count(), 0)
        self.assertEqual(mine.status, 'interview_scheduled')

    def test_string_arguments_are_converted_safely(self):
        """ADK does not convert types: the string "false" must not count as True."""
        c = self.candidate()
        self.mocked(90)
        scripts = dict(DEFAULT_SCRIPTS)
        scripts[MATCHING] = [('record_match_decision', {'is_match': 'false', 'match_percent': '250', 'reason': 'x' * 900})]
        run = llm_pipeline.run_llm_pipeline(c, self.job, model=ScriptedLlm(scripts=scripts))
        self.assertFalse(run.qualified)
        self.assertEqual(run.results['matching']['match_percent'], 100)
        reason = AgentLog.objects.get(candidate=c, from_agent=MATCHING).message
        self.assertLess(len(reason), 330)

    def test_helpers(self):
        self.assertEqual([llm_pipeline._bool(v) for v in (True, 'true', 'False', 'no', 0, 'yes')],
                         [True, True, False, False, False, True])
        self.assertEqual([llm_pipeline._int(v, 5, 1, 8) for v in ('3', 99, -4, 'abc', None, 2.9)], [3, 8, 1, 5, 5, 2])

    def test_search_rejects_a_tiny_query_and_clamps_top_k(self):
        c = self.candidate()
        run = PipelineRun(candidate=c, job=self.job)
        self.assertEqual(llm_pipeline._search(run, ' a ', 5)['status'], 'error')
        result = llm_pipeline._search(run, 'django', 'many')
        self.assertEqual(result['status'], 'success')
        self.assertLessEqual(len(result['chunks']), 5)
        self.assertTrue(all(len(ch['text']) <= 500 for ch in result['chunks']))

    def test_search_keeps_at_most_six_extra_chunks(self):
        resume = '\n'.join(f'Section {i}\nwork {i} ' * 3 for i in range(12))
        c = self.candidate(resume=resume)
        run = PipelineRun(candidate=c, job=self.job)
        llm_pipeline._search(run, 'work', 8)
        llm_pipeline._search(run, 'section', 8)
        self.assertLessEqual(len(run.extra_chunks), 6)


class FallbackTests(Base):
    def final(self, c):
        c.refresh_from_db()
        return c.status, c.score

    def test_model_failure_is_logged_and_the_builtin_steps_finish_the_run(self):
        c = self.candidate()
        self.mocked(90)
        model = ScriptedLlm(fail_for=[MATCHING])
        run = llm_pipeline.run_llm_pipeline(c, self.job, model=model)
        self.assertEqual(self.final(c), ('interview_scheduled', 90))
        self.assertTrue(run.llm_failed)
        self.match_mock.assert_called_once()                       # built-in matching asked Gemini itself
        self.assertNotIn(RANKING, model.calls)                     # later agents did not call the dead model
        self.assertNotIn(SCHEDULING, model.calls)
        messages = [m for _a, _b, m in self.logs(c)]
        self.assertTrue(any('The model call failed (RuntimeError: quota exhausted)' in m for m in messages))
        self.assertEqual(sum('Rank #1' in m for m in messages), 1)
        self.assertEqual(AgentLog.objects.filter(candidate=c, to_agent='Recruiter Dashboard').count(), 1)
        # the failure is logged once, where it happened, with no extra note for every later step
        self.assertEqual(sum('The model call failed' in m for m in messages), 1)
        self.assertFalse(any(to == 'Pipeline' and 'did not complete' in m for _a, to, m in self.logs(c)))

    def test_an_agent_that_never_calls_its_tool_is_completed_by_the_builtin_step(self):
        c = self.candidate()
        self.mocked(90)
        run = llm_pipeline.run_llm_pipeline(c, self.job, model=ScriptedLlm(scripts={}))
        self.assertEqual(self.final(c), ('interview_scheduled', 90))
        self.assertEqual(run.done, {'screening', 'matching', 'ranking', 'scheduling'})
        notes = [m for _a, to, m in self.logs(c) if to == 'Pipeline']
        self.assertEqual(len(notes), 4)
        self.assertTrue(all('did not complete this step' in n for n in notes))

    @override_settings(PIPELINE_LLM_BUDGET_SECONDS=-1)
    def test_an_used_up_time_budget_stops_all_model_calls(self):
        c = self.candidate()
        self.mocked(90)
        model = ScriptedLlm()
        run = llm_pipeline.run_llm_pipeline(c, self.job, model=model)
        self.assertEqual(model.calls, [])
        self.assertEqual(self.final(c), ('interview_scheduled', 90))
        self.assertTrue(any('time budget' in m for _a, _b, m in self.logs(c)))
        self.assertTrue(run.llm_failed)

    def test_a_failing_tool_is_logged_and_the_run_continues(self):
        c = self.candidate()
        self.mocked(90)
        with mock.patch('api.adk_pipeline_llm.rag.semantic_chunks', side_effect=RuntimeError('db down')):
            llm_pipeline.run_llm_pipeline(c, self.job, model=ScriptedLlm())
        self.assertEqual(self.final(c), ('interview_scheduled', 90))
        failures = [m for _a, to, m in self.logs(c) if 'Tool failed' in m]
        self.assertEqual(len(failures), 2)                         # both search calls
        self.assertIn('RuntimeError: db down', failures[0])

    def test_an_adk_crash_still_finishes_the_run(self):
        c = self.candidate()
        self.mocked(90)
        with mock.patch('api.adk_pipeline_llm.run_agent_sync', side_effect=RuntimeError('adk broke')):
            run = llm_pipeline.run_llm_pipeline(c, self.job, model=ScriptedLlm())
        self.assertEqual(self.final(c), ('interview_scheduled', 90))
        self.assertEqual(run.done, {'screening', 'matching', 'ranking', 'scheduling'})
        self.assertTrue(any('hit an error' in m for _a, _b, m in self.logs(c)))


class EngineSelectionTests(Base):
    def run_full(self, c):
        return run_full_pipeline(c.pk, self.job.pk)

    def test_llm_mode_with_a_key_uses_the_llm_agents(self):
        c = self.candidate()
        self.mocked(90)
        model = ScriptedLlm()
        with override_settings(PIPELINE_AGENT_MODE='llm', GEMINI_API_KEY='k'), \
                mock.patch('api.adk_pipeline_llm.make_model', return_value=model):
            candidate, slot = self.run_full(c)
        self.assertTrue(model.calls)
        self.assertEqual(candidate.status, 'interview_scheduled')
        self.assertIsNotNone(slot)
        app = Application.objects.get(candidate=c, job=self.job)
        self.assertEqual((app.status, app.score), ('interview_scheduled', 90))

    def test_without_a_key_the_fixed_agents_are_used(self):
        c = self.candidate()
        self.mocked(90)
        with override_settings(PIPELINE_AGENT_MODE='llm', GEMINI_API_KEY=''), \
                mock.patch('api.adk_pipeline_llm.run_llm_pipeline') as llm_engine:
            candidate, _slot = self.run_full(c)
        llm_engine.assert_not_called()
        self.assertEqual(candidate.status, 'interview_scheduled')

    def test_deterministic_mode_skips_the_llm_agents(self):
        c = self.candidate()
        self.mocked(90)
        with override_settings(PIPELINE_AGENT_MODE='deterministic', GEMINI_API_KEY='k'), \
                mock.patch('api.adk_pipeline_llm.run_llm_pipeline') as llm_engine:
            self.run_full(c)
        llm_engine.assert_not_called()

    def test_tests_default_to_deterministic_so_a_real_key_is_never_used(self):
        from django.conf import settings
        self.assertEqual(settings.PIPELINE_AGENT_MODE, 'deterministic')

    def test_llm_and_fixed_agents_give_the_same_result(self):
        results = []
        for mode, name in (('llm', 'ViaLlm'), ('deterministic', 'ViaFixed')):
            c = self.candidate(name)
            self.mocked(85)
            with override_settings(PIPELINE_AGENT_MODE=mode, GEMINI_API_KEY='k'), \
                    mock.patch('api.adk_pipeline_llm.make_model', return_value=ScriptedLlm()):
                candidate, slot = self.run_full(c)
            app = Application.objects.get(candidate=c, job=self.job)
            results.append((candidate.status, candidate.score, slot is not None, app.status, app.score))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 'interview_scheduled')


class FixedAgentsLogThroughCallbacksTests(Base):
    def test_fixed_agents_have_the_log_callback(self):
        pipeline = build_pipeline(PipelineRun(candidate=None, job=None))
        self.assertTrue(all(a.after_agent_callback for a in pipeline.sub_agents))

    def test_fixed_agents_queue_their_log_and_the_callback_writes_it_once(self):
        from .adk_pipeline import run_adk_pipeline
        c = self.candidate()
        self.mocked(90)
        run_adk_pipeline(c, self.job)
        self.assertEqual(self.edges(c), [(SCREENING, MATCHING), (MATCHING, RANKING), (RANKING, SCHEDULING),
                                         (SCHEDULING, 'Recruiter Dashboard')])


class MergeChunksTests(TestCase):
    def chunk(self, i):
        return {'chunk_id': i, 'section': 's', 'text': f't{i}', 'similarity': None}

    def test_adds_new_chunks_without_duplicates(self):
        from .rag import merge_chunks
        merged = merge_chunks({'mode': 'semantic', 'chunks': [self.chunk(1), self.chunk(2)]},
                              [self.chunk(2), self.chunk(3)])
        self.assertEqual([c['chunk_id'] for c in merged['chunks']], [1, 2, 3])
        self.assertEqual(merged['mode'], 'semantic')

    def test_respects_the_limit_and_skips_the_whole_resume_pseudo_chunk(self):
        from .rag import merge_chunks
        merged = merge_chunks({'mode': 'x', 'chunks': [self.chunk(1)]},
                              [self.chunk(0), self.chunk(5), self.chunk(6), self.chunk(7)], limit=3)
        self.assertEqual([c['chunk_id'] for c in merged['chunks']], [1, 5, 6])

    def test_tolerates_evidence_without_chunks_and_does_not_change_its_input(self):
        from .rag import merge_chunks
        original = {'mode': 'semantic'}
        self.assertEqual(merge_chunks(original, [self.chunk(4)])['chunks'], [self.chunk(4)])
        self.assertEqual(original, {'mode': 'semantic'})
