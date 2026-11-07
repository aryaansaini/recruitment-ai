"""The MCP servers (official SDK), the client, and the pipeline going through them.

Most tests talk to the servers over the real MCP protocol through an in-memory transport, so the tool
schemas, argument validation and error reporting are the real ones, while the database is the test
database. A few tests start the servers as real child processes over stdio.
"""
import asyncio
import base64
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from mcp.shared.memory import create_connected_server_and_client_session

from . import mcp_client
from .agents import (_rule_based_score, ats_mcp_update, calendar_mcp, resume_parser_mcp, resume_screening_agent,
                     run_full_pipeline)
from .llm import LLMError
from .mcp_servers import ats_server, calendar_server, resume_parser_server
from .models import AgentLog, Application, Candidate, Job
from .resume_parser import keyword_summary
from .tests_adk_llm_pipeline import ScriptedLlm

User = get_user_model()

FACTORIES = {
    'ats': ats_server.create_server,
    'calendar': calendar_server.create_server,
    'resume_parser': lambda owner_id=None: resume_parser_server.create_server(),
}


class Recording:
    """Wraps a client session and notes every tool call."""

    def __init__(self, session, server, calls):
        self.session, self.server, self.calls = session, server, calls

    async def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((self.server, name, dict(arguments or {})))
        return await self.session.call_tool(name, arguments, **kwargs)

    def __getattr__(self, item):
        return getattr(self.session, item)


def in_memory_transport(calls):
    @asynccontextmanager
    async def open_session(server, owner_id=None):
        async with create_connected_server_and_client_session(FACTORIES[server](owner_id)._mcp_server) as session:
            yield Recording(session, server, calls)
    return open_session


class McpBase(TestCase):
    def setUp(self):
        cache.clear()
        self.calls = []
        patcher = mock.patch('api.mcp_client.open_session', in_memory_transport(self.calls))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.alice = User.objects.create_user('alice_mcp', password='Str0ng!pass-93')
        self.bob = User.objects.create_user('bob_mcp', password='Str0ng!pass-93')
        self.job = Job.objects.create(title='Backend', required_skills='python, django', min_experience=1,
                                      description='Build APIs', owner=self.alice)

    def candidate(self, owner=None, name='Asha', score=0, status='pending', **extra):
        return Candidate.objects.create(name=name, email=f'{name.lower()}@example.com', phone='1', owner=owner or self.alice,
                                        skills='python, django', experience=3, score=score, status=status,
                                        resume_text='Skills\npython django\nExperience\nBuilt REST APIs.', **extra)

    def call(self, server, tool, arguments=None, owner=None):
        owner = owner or self.alice
        return mcp_client.call_tool(server, tool, arguments, owner_id=None if server == 'resume_parser' else owner.pk)

    def with_session(self, server, fn, owner=None):
        async def run():
            async with mcp_client.open_session(server, (owner or self.alice).pk) as session:
                return await fn(session)
        return async_to_sync(run)()


# ----------------------------------------------------------------------------------------------------------
# ATS server
# ----------------------------------------------------------------------------------------------------------
class AtsServerTests(McpBase):
    def test_lists_its_tools_with_schemas(self):
        tools = self.with_session('ats', lambda s: s.list_tools()).tools
        by_name = {t.name: t for t in tools}
        self.assertEqual(set(by_name), {'update_candidate_status', 'get_candidate', 'list_candidates'})
        schema = by_name['update_candidate_status'].inputSchema
        self.assertEqual(set(schema['required']), {'candidate_id', 'status'})
        self.assertEqual(set(schema['properties']), {'candidate_id', 'status', 'score', 'interview_date'})
        self.assertTrue(all(t.description for t in tools))

    def test_update_saves_status_score_and_interview_time(self):
        c = self.candidate()
        message = self.call('ats', 'update_candidate_status',
                            {'candidate_id': c.pk, 'status': 'interview_scheduled', 'score': 88.5,
                             'interview_date': '2026-11-09T10:00:00+00:00'})
        self.assertEqual(message, 'ATS Updated: Asha -> interview_scheduled')
        c.refresh_from_db()
        self.assertEqual((c.status, c.score), ('interview_scheduled', 88.5))
        self.assertEqual(c.interview_date, datetime(2026, 11, 9, 10, tzinfo=dt_timezone.utc))

    def test_status_only_leaves_the_other_fields_alone(self):
        when = timezone.now().replace(microsecond=0)
        c = self.candidate(score=70, interview_date=when)
        self.call('ats', 'update_candidate_status', {'candidate_id': c.pk, 'status': 'ranked'})
        c.refresh_from_db()
        self.assertEqual((c.status, c.score, c.interview_date), ('ranked', 70, when))

    def test_a_time_without_zone_is_read_as_utc(self):
        c = self.candidate()
        self.call('ats', 'update_candidate_status',
                  {'candidate_id': c.pk, 'status': 'ranked', 'interview_date': '2026-11-09T10:00:00'})
        c.refresh_from_db()
        self.assertEqual(c.interview_date, datetime(2026, 11, 9, 10, tzinfo=dt_timezone.utc))

    def test_bad_input_is_refused_and_nothing_changes(self):
        c = self.candidate(score=10)
        for args, text in [({'status': 'hired'}, 'status must be one of'),
                           ({'status': 'ranked', 'score': 101}, 'score must be a number from 0 to 100'),
                           ({'status': 'ranked', 'score': -1}, 'score must be a number from 0 to 100'),
                           ({'status': 'ranked', 'interview_date': 'next tuesday'}, 'ISO 8601')]:
            with self.assertRaises(mcp_client.McpToolError) as ctx:
                self.call('ats', 'update_candidate_status', {'candidate_id': c.pk, **args})
            self.assertIn(text, str(ctx.exception))
        c.refresh_from_db()
        self.assertEqual((c.status, c.score, c.interview_date), ('pending', 10, None))

    def test_wrong_argument_types_are_refused_by_the_protocol(self):
        with self.assertRaises(mcp_client.McpToolError):
            self.call('ats', 'update_candidate_status', {'candidate_id': 'abc', 'status': 'ranked'})

    def test_another_recruiters_candidate_is_not_found(self):
        theirs = self.candidate(owner=self.bob, name='Ben')
        with self.assertRaises(mcp_client.McpToolError) as ctx:
            self.call('ats', 'update_candidate_status', {'candidate_id': theirs.pk, 'status': 'rejected'})
        self.assertIn('Candidate not found', str(ctx.exception))
        for tool in ('get_candidate',):
            with self.assertRaises(mcp_client.McpToolError):
                self.call('ats', tool, {'candidate_id': theirs.pk})
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, 'pending')
        with self.assertRaises(mcp_client.McpToolError):
            self.call('ats', 'get_candidate', {'candidate_id': 999999})

    def test_get_candidate_returns_the_record_without_the_resume_text(self):
        c = self.candidate(score=64)
        record = self.call('ats', 'get_candidate', {'candidate_id': c.pk})
        self.assertEqual((record['name'], record['score'], record['status']), ('Asha', 64, 'pending'))
        self.assertEqual(record['skills'], 'python, django')
        self.assertEqual(record['resume_characters'], len(c.resume_text))
        self.assertNotIn('resume_text', record)

    def test_list_candidates_is_scoped_ordered_and_filterable(self):
        low = self.candidate(name='Low', score=10, status='rejected')
        high = self.candidate(name='High', score=90, status='ranked')
        self.candidate(owner=self.bob, name='Other', score=99)
        names = [c['name'] for c in self.call('ats', 'list_candidates')]
        self.assertEqual(names, ['High', 'Low'])
        self.assertEqual([c['id'] for c in self.call('ats', 'list_candidates', {'status': 'rejected'})], [low.pk])
        self.assertEqual(len(self.call('ats', 'list_candidates', {'limit': 1})), 1)
        self.assertEqual([c['id'] for c in self.call('ats', 'list_candidates', {'limit': 0})], [high.pk])   # clamped to 1
        with self.assertRaises(mcp_client.McpToolError):
            self.call('ats', 'list_candidates', {'status': 'hired'})

    def test_the_server_refuses_to_start_without_an_owner(self):
        from .mcp_servers._common import owner_id_from_env
        with mock.patch.dict('os.environ', {'MCP_OWNER_ID': ''}), self.assertRaises(SystemExit):
            owner_id_from_env()
        with mock.patch.dict('os.environ', {'MCP_OWNER_ID': 'abc'}), self.assertRaises(SystemExit):
            owner_id_from_env()
        with mock.patch.dict('os.environ', {'MCP_OWNER_ID': '7'}):
            self.assertEqual(owner_id_from_env(), 7)


# ----------------------------------------------------------------------------------------------------------
# Resume parser server
# ----------------------------------------------------------------------------------------------------------
class ResumeParserServerTests(McpBase):
    TEXT = 'Asha Rao\nasha@example.com\n+91 98765 43210\nSkills\nPython Django SQL\nExperience\nBuilt APIs.'

    def test_lists_its_tools(self):
        tools = self.with_session('resume_parser', lambda s: s.list_tools()).tools
        self.assertEqual({t.name for t in tools}, {'extract_resume_text', 'analyze_resume', 'chunk_resume_text'})

    def test_analyze_resume_matches_the_keyword_summary_and_adds_contact_details(self):
        result = self.call('resume_parser', 'analyze_resume', {'text': self.TEXT})
        expected = keyword_summary(self.TEXT)
        self.assertEqual({k: result[k] for k in expected}, expected)
        self.assertEqual(result['keywords_found'], ['python', 'django', 'sql'])
        self.assertEqual(result['email'], 'asha@example.com')
        self.assertEqual(result['phone'], '+919876543210')

    def test_empty_text_is_refused(self):
        for tool in ('analyze_resume', 'chunk_resume_text'):
            with self.assertRaises(mcp_client.McpToolError):
                self.call('resume_parser', tool, {'text': '   '})

    def test_extract_resume_text_reads_a_base64_file(self):
        data = base64.b64encode(b'Skills\nPython and Django\n').decode()
        self.assertEqual(self.call('resume_parser', 'extract_resume_text', {'filename': 'cv.txt', 'content_base64': data}),
                         'Skills\nPython and Django')

    def test_extract_resume_text_refuses_bad_files(self):
        ok = base64.b64encode(b'hello world').decode()
        for filename, content, text in [('cv.exe', ok, 'Only PDF, DOCX or TXT'),
                                        ('cv.txt', '***not base64***', 'not valid base64'),
                                        ('cv.txt', base64.b64encode(b'   ').decode(), 'No text found'),
                                        ('cv.txt', 'A' * (5 * 1024 * 1024 * 4 // 3 + 100), 'larger than 5 MB')]:
            with self.assertRaises(mcp_client.McpToolError) as ctx:
                self.call('resume_parser', 'extract_resume_text', {'filename': filename, 'content_base64': content})
            self.assertIn(text, str(ctx.exception))

    def test_chunks_have_sections_and_skip_the_contact_header(self):
        chunks = self.call('resume_parser', 'chunk_resume_text', {'text': self.TEXT})
        self.assertTrue(chunks)
        self.assertEqual({c['section'] for c in chunks} & {'header'}, set())
        self.assertTrue(all({'section', 'index', 'text'} <= set(c) for c in chunks))
        self.assertNotIn('asha@example.com', ' '.join(c['text'] for c in chunks))

    def test_upload_limit_is_shared_with_the_api(self):
        from .views import MAX_RESUME_BYTES
        from .resume_parser import MAX_RESUME_BYTES as PARSER_LIMIT
        self.assertEqual(MAX_RESUME_BYTES, PARSER_LIMIT)


# ----------------------------------------------------------------------------------------------------------
# Calendar server
# ----------------------------------------------------------------------------------------------------------
class CalendarServerTests(McpBase):
    FRIDAY = datetime(2026, 11, 6, 12, 30, tzinfo=dt_timezone.utc)

    def test_lists_its_tools(self):
        tools = self.with_session('calendar', lambda s: s.list_tools()).tools
        self.assertEqual({t.name for t in tools}, {'propose_interview_slot', 'find_free_slots', 'list_booked_slots'})

    def test_propose_slot_gives_the_same_slot_as_the_in_process_version(self):
        slot = self.call('calendar', 'propose_interview_slot', {'candidate_name': 'Asha Rao'})
        local = calendar_mcp('Asha Rao')                         # MCP is off in tests: the built-in version
        self.assertEqual((slot['candidate'], slot['platform'], slot['link']),
                         (local['candidate'], local['platform'], local['link']))
        self.assertEqual(slot['link'], 'meet.google.com/recruitment-asha-rao')
        when = parse_datetime(slot['datetime'])
        self.assertTrue(timezone.is_aware(when))
        self.assertLess(abs(when - local['datetime']), timedelta(minutes=2))
        self.assertLess(abs(when - (timezone.now() + timedelta(days=3))), timedelta(minutes=2))
        self.assertEqual(slot['slot'], when.strftime('%Y-%m-%d %H:%M'))

    def test_free_slots_start_the_next_weekday_morning_and_skip_the_weekend(self):
        slots = calendar_server.free_slots(self.alice.pk, count=3, now=self.FRIDAY)
        self.assertEqual([s['slot'] for s in slots], ['2026-11-09 10:00', '2026-11-09 11:00', '2026-11-09 12:00'])

    def test_free_slots_stay_inside_working_hours_on_weekdays(self):
        slots = calendar_server.free_slots(self.alice.pk, count=20, days_ahead=14, now=self.FRIDAY)
        times = [parse_datetime(s['datetime']) for s in slots]
        self.assertEqual(len(times), 20)
        self.assertTrue(all(t.weekday() < 5 and 10 <= t.hour <= 16 for t in times))
        self.assertEqual(times, sorted(times))

    def test_free_slots_skip_booked_times_of_this_recruiter_only(self):
        self.candidate(name='Booked', interview_date=datetime(2026, 11, 9, 10, 20, tzinfo=dt_timezone.utc))
        self.candidate(owner=self.bob, name='Theirs', interview_date=datetime(2026, 11, 9, 11, tzinfo=dt_timezone.utc))
        slots = calendar_server.free_slots(self.alice.pk, count=2, now=self.FRIDAY)
        self.assertEqual([s['slot'] for s in slots], ['2026-11-09 11:00', '2026-11-09 12:00'])   # 10:00 is taken

    def test_free_slots_clamp_their_arguments(self):
        self.assertEqual(len(calendar_server.free_slots(self.alice.pk, count=0, now=self.FRIDAY)), 1)
        self.assertEqual(len(calendar_server.free_slots(self.alice.pk, count=500, days_ahead=500, now=self.FRIDAY)), 20)

    def test_booked_slots_are_scoped_ordered_and_limited_to_the_range(self):
        now = timezone.now().replace(microsecond=0)
        later = self.candidate(name='Later', interview_date=now + timedelta(days=5))
        soon = self.candidate(name='Soon', interview_date=now + timedelta(days=1))
        self.candidate(name='Far', interview_date=now + timedelta(days=40))
        self.candidate(name='Past', interview_date=now - timedelta(days=1))
        self.candidate(owner=self.bob, name='Theirs', interview_date=now + timedelta(days=2))
        booked = calendar_server.booked_slots(self.alice.pk, days_ahead=14, now=now)
        self.assertEqual([b['candidate'] for b in booked], ['Soon', 'Later'])
        self.assertEqual([b['candidate_id'] for b in booked], [soon.pk, later.pk])

    def test_free_and_booked_slots_work_over_mcp_and_are_scoped(self):
        self.candidate(name='Soon', interview_date=timezone.now() + timedelta(days=1))
        self.candidate(owner=self.bob, name='Theirs', interview_date=timezone.now() + timedelta(days=1))
        self.assertEqual([b['candidate'] for b in self.call('calendar', 'list_booked_slots')], ['Soon'])
        self.assertEqual(len(self.call('calendar', 'find_free_slots', {'count': 4})), 4)


# ----------------------------------------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------------------------------------
class ClientTests(McpBase):
    def result(self, text=None, structured=None, error=False):
        content = [SimpleNamespace(type='text', text=text)] if text is not None else []
        return SimpleNamespace(content=content, structuredContent=structured, isError=error)

    def test_unwrap_handles_every_result_shape(self):
        self.assertEqual(mcp_client.unwrap(self.result(structured={'result': 42})), 42)
        self.assertEqual(mcp_client.unwrap(self.result(structured={'a': 1, 'result': 2})), {'a': 1, 'result': 2})
        self.assertEqual(mcp_client.unwrap(self.result(text='{"a": 1}')), {'a': 1})
        self.assertEqual(mcp_client.unwrap(self.result(text='plain words')), 'plain words')
        self.assertIsNone(mcp_client.unwrap(self.result()))

    def test_unwrap_turns_a_tool_error_into_mcp_tool_error(self):
        with self.assertRaises(mcp_client.McpToolError) as ctx:
            mcp_client.unwrap(self.result(text='Error executing tool update_candidate_status: Candidate not found.', error=True))
        self.assertEqual(str(ctx.exception), 'update_candidate_status: Candidate not found.')

    def test_tool_errors_are_a_kind_of_mcp_error(self):
        self.assertTrue(issubclass(mcp_client.McpToolError, mcp_client.McpError))

    def test_unknown_server_is_an_error(self):
        with self.assertRaises(mcp_client.McpError):
            mcp_client.call_tool('payroll', 'pay', {})

    def test_a_server_that_cannot_be_reached_is_an_mcp_error_not_a_tool_error(self):
        @asynccontextmanager
        async def broken(server, owner_id=None):
            raise OSError('no such file')
            yield
        with mock.patch('api.mcp_client.open_session', broken):
            with self.assertRaises(mcp_client.McpError) as ctx:
                mcp_client.call_tool('ats', 'list_candidates', owner_id=self.alice.pk)
        self.assertNotIsInstance(ctx.exception, mcp_client.McpToolError)
        self.assertIn('OSError', str(ctx.exception))

    def test_a_slow_server_times_out(self):
        @asynccontextmanager
        async def slow(server, owner_id=None):
            await asyncio.sleep(5)
            yield
        with mock.patch('api.mcp_client.open_session', slow):
            with self.assertRaises(mcp_client.McpError):
                mcp_client.call_tool('ats', 'list_candidates', owner_id=self.alice.pk, timeout=0.1)

    def test_the_child_process_gets_the_owner_and_the_parents_environment(self):
        with mock.patch.dict('os.environ', {'DATABASE_URL': 'postgres://x', 'MCP_OWNER_ID': '999'}):
            env = mcp_client.child_env(5)
            self.assertEqual((env['MCP_OWNER_ID'], env['DATABASE_URL']), ('5', 'postgres://x'))
            self.assertIn('DJANGO_SETTINGS_MODULE', env)
            self.assertNotIn('MCP_OWNER_ID', mcp_client.child_env(None))      # a stale value is never inherited

    def test_calling_from_inside_a_running_event_loop_works(self):
        async def inside_a_loop():
            return mcp_client.call_tool('resume_parser', 'analyze_resume', {'text': 'python and sql'})
        self.assertEqual(asyncio.run(inside_a_loop())['keywords_found'], ['python', 'sql'])


# ----------------------------------------------------------------------------------------------------------
# Real child processes over stdio (no database needed)
# ----------------------------------------------------------------------------------------------------------
class RealStdioTests(SimpleTestCase):
    def test_resume_parser_server_runs_as_a_child_process(self):
        result = mcp_client.call_tool('resume_parser', 'analyze_resume', {'text': 'Python and SQL ai@example.com'})
        self.assertEqual((result['keywords_found'], result['email']), (['python', 'sql'], 'ai@example.com'))

    def test_all_three_servers_start_and_list_their_tools_over_stdio(self):
        async def names(server, owner):
            async with mcp_client.open_session(server, owner) as session:
                return {t.name for t in (await session.list_tools()).tools}
        self.assertEqual(asyncio.run(names('ats', 1)), {'update_candidate_status', 'get_candidate', 'list_candidates'})
        self.assertEqual(asyncio.run(names('calendar', 1)), {'propose_interview_slot', 'find_free_slots', 'list_booked_slots'})
        self.assertEqual(asyncio.run(names('resume_parser', None)),
                         {'extract_resume_text', 'analyze_resume', 'chunk_resume_text'})

    def test_ats_and_calendar_servers_do_not_start_without_an_owner(self):
        for server, tool in (('ats', 'list_candidates'), ('calendar', 'list_booked_slots')):
            with self.assertRaises(mcp_client.McpError):
                mcp_client.call_tool(server, tool, owner_id=None, timeout=20)


# ----------------------------------------------------------------------------------------------------------
# The pipeline through the MCP servers
# ----------------------------------------------------------------------------------------------------------
class PipelineThroughMcpTests(McpBase):
    def setUp(self):
        super().setUp()
        self.card = {'overall': 90, 'summary': 'ok', 'retrieval': 'semantic', 'evidence': [],
                     'breakdown': {'skills': 90, 'experience': 100, 'education': 50}}
        self.verdict = SimpleNamespace(is_match=True, match_percent=70, reason='built-in match')
        stack = ExitStack()
        stack.enter_context(mock.patch('api.agents.rag.retrieve_evidence', return_value={'mode': 'semantic', 'chunks': []}))
        self.score_mock = stack.enter_context(mock.patch('api.agents.rag.score_candidate', return_value=self.card))
        stack.enter_context(mock.patch('api.agents.generate_structured', return_value=self.verdict))
        self.addCleanup(stack.close)

    def outcome(self, c):
        db = Candidate.objects.get(pk=c.pk)
        app = Application.objects.get(candidate=c, job=self.job)
        return (db.status, db.score, db.interview_date is not None, app.status, app.score, app.interview_date is not None)

    def ats_statuses(self):
        return [args['status'] for server, tool, args in self.calls if tool == 'update_candidate_status']

    def test_mcp_is_off_in_tests_unless_a_test_turns_it_on(self):
        self.assertFalse(settings.USE_MCP)
        run_full_pipeline(self.candidate().pk, self.job.pk)
        self.assertEqual(self.calls, [])

    def run_both_ways(self, **settings_kwargs):
        results = []
        for use_mcp, name in ((False, 'Local'), (True, 'ViaMcp')):
            c = self.candidate(name=name)
            with override_settings(USE_MCP=use_mcp, **settings_kwargs):
                run_full_pipeline(c.pk, self.job.pk)
            results.append(self.outcome(c))
        return results

    def test_adk_pipeline_gives_the_same_result_through_mcp(self):
        local, via_mcp = self.run_both_ways(PIPELINE_USE_ADK=True)
        self.assertEqual(local, via_mcp)
        self.assertEqual(via_mcp, ('interview_scheduled', 90, True, 'interview_scheduled', 90, True))

    def test_plain_pipeline_gives_the_same_result_through_mcp(self):
        local, via_mcp = self.run_both_ways(PIPELINE_USE_ADK=False)
        self.assertEqual(local, via_mcp)
        self.assertEqual(via_mcp[0], 'interview_scheduled')

    def test_the_servers_really_were_called(self):
        c = self.candidate()
        with override_settings(USE_MCP=True):
            run_full_pipeline(c.pk, self.job.pk)
        self.assertEqual(self.ats_statuses(), ['shortlisted', 'matched', 'ranked', 'interview_scheduled'])
        self.assertEqual([(s, t) for s, t, _a in self.calls if s == 'calendar'], [('calendar', 'propose_interview_slot')])
        self.assertTrue(all(a['candidate_id'] == c.pk for s, t, a in self.calls if s == 'ats'))

    def test_the_score_is_saved_by_the_ats_server(self):
        c = self.candidate()
        with override_settings(USE_MCP=True):
            resume_screening_agent(c, self.job)           # no candidate.save() on this path any more
        self.assertEqual(Candidate.objects.get(pk=c.pk).score, 90)
        self.assertEqual(Candidate.objects.get(pk=c.pk).status, 'shortlisted')

    def test_rejected_candidate_only_gets_the_rejected_update(self):
        self.card['overall'] = 20
        c = self.candidate()
        with override_settings(USE_MCP=True):
            run_full_pipeline(c.pk, self.job.pk)
        self.assertEqual(self.ats_statuses(), ['rejected'])
        self.assertFalse([1 for s, *_ in self.calls if s == 'calendar'])
        self.assertEqual(Candidate.objects.get(pk=c.pk).status, 'rejected')

    def test_llm_agents_also_go_through_mcp(self):
        c = self.candidate()
        from .adk_pipeline_llm import run_llm_pipeline
        with override_settings(USE_MCP=True):
            run_llm_pipeline(c, self.job, model=ScriptedLlm())
        self.assertEqual(self.ats_statuses(), ['shortlisted', 'matched', 'ranked', 'interview_scheduled'])
        self.assertEqual(Candidate.objects.get(pk=c.pk).status, 'interview_scheduled')

    def test_an_unavailable_server_falls_back_to_in_process_and_the_run_completes(self):
        @asynccontextmanager
        async def down(server, owner_id=None):
            raise ConnectionError('server did not start')
            yield
        c = self.candidate()
        with override_settings(USE_MCP=True), mock.patch('api.mcp_client.open_session', down), \
                self.assertLogs('api.agents', level='WARNING') as logs:
            run_full_pipeline(c.pk, self.job.pk)
        self.assertEqual(self.outcome(c), ('interview_scheduled', 90, True, 'interview_scheduled', 90, True))
        self.assertTrue(any('unavailable' in line for line in logs.output))

    def test_a_tool_that_refuses_the_request_is_not_hidden_by_a_fallback(self):
        c = self.candidate()
        with override_settings(USE_MCP=True), \
                mock.patch('api.mcp_client.call_tool', side_effect=mcp_client.McpToolError('Candidate not found.')):
            with self.assertRaises(mcp_client.McpToolError):
                ats_mcp_update(c, 'ranked')

    def test_a_candidate_without_an_owner_never_uses_mcp(self):
        c = self.candidate()
        Candidate.objects.filter(pk=c.pk).update(owner=None)
        c.refresh_from_db()
        with override_settings(USE_MCP=True):
            ats_mcp_update(c, 'ranked')
        self.assertEqual(self.calls, [])
        self.assertEqual(Candidate.objects.get(pk=c.pk).status, 'ranked')

    def test_the_rule_based_fallback_uses_the_resume_parser_server(self):
        c = self.candidate()
        self.score_mock.side_effect = LLMError('quota')
        with override_settings(USE_MCP=False):
            local = _rule_based_score(c)
        with override_settings(USE_MCP=True):
            via_mcp = _rule_based_score(c)
        self.assertEqual(local, via_mcp)
        self.assertEqual([(s, t) for s, t, _a in self.calls], [('resume_parser', 'analyze_resume')])

    def test_resume_parser_stand_in_returns_the_same_keys_either_way(self):
        text = 'Python Django SQL and more'
        local = resume_parser_mcp(text)
        with override_settings(USE_MCP=True):
            via_mcp = resume_parser_mcp(text)
        self.assertEqual({k: via_mcp[k] for k in local}, local)
