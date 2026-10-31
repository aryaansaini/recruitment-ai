"""Google ADK orchestration of the four recruitment agents.

Each agent is a small ADK agent (a BaseAgent subclass) that runs the project's existing, tested
logic from `api.agents`. An ADK `SequentialAgent` runs them in order, and the ADK session records
what each agent decided. Agents 3 and 4 skip themselves for candidates who did not qualify.

Why custom agents and not LlmAgents: agents 1 and 2 keep using the project's own Gemini client,
which gives verified resume quotes, fixed score weights and a clearly flagged rule-based fallback.
"""
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from asgiref.sync import sync_to_async
from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from typing_extensions import override

from . import agents as pipeline_steps
from .adk_utils import run_agent_sync


@dataclass
class PipelineRun:
    """Objects shared by the four agents during one run (the ORM objects cannot live in ADK state)."""
    candidate: Any
    job: Any
    qualified: bool = False
    slot: dict | None = None
    session_state: dict = field(default_factory=dict)


class _PipelineStep(BaseAgent):
    """Base class: runs `work()` in a worker thread (Django's ORM must not run in the event loop)."""
    model_config = {'arbitrary_types_allowed': True}
    pipeline: Any = None

    def work(self) -> tuple[str, dict]:
        raise NotImplementedError

    @override
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        message, state_delta = await sync_to_async(self.work)()
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role='model', parts=[types.Part(text=message)]),
            actions=EventActions(state_delta=state_delta),
        )


class ScreeningAgent(_PipelineStep):
    def work(self):
        run = self.pipeline
        run.candidate = pipeline_steps.resume_screening_agent(run.candidate, run.job)
        c = run.candidate
        return (f'Screened {c.name}: score {c.score}, status {c.status}',
                {'screening_done': True, 'score': float(c.score), 'status': c.status})


class MatchingAgent(_PipelineStep):
    def work(self):
        run = self.pipeline
        run.qualified = bool(pipeline_steps.job_matching_agent(run.candidate, run.job))
        verdict = 'matched' if run.qualified else 'not matched'
        return (f'Matching: {run.candidate.name} {verdict}',
                {'matching_done': True, 'qualified': run.qualified})


class RankingAgent(_PipelineStep):
    def work(self):
        run = self.pipeline
        if not run.qualified:
            return ('Ranking skipped: candidate did not qualify.', {'ranking_skipped': True})
        pipeline_steps.candidate_ranking_agent([run.candidate], job=run.job)
        return (f'Ranked {run.candidate.name} for {run.job.title}', {'ranking_done': True})


class SchedulingAgent(_PipelineStep):
    def work(self):
        run = self.pipeline
        if not run.qualified:
            return ('Scheduling skipped: candidate did not qualify.', {'scheduling_skipped': True})
        run.slot = pipeline_steps.interview_coordination_agent(run.candidate)
        return (f"Interview proposed for {run.slot['slot']}", {'scheduling_done': True})


def build_pipeline(run: PipelineRun) -> SequentialAgent:
    return SequentialAgent(
        name='recruitment_pipeline',
        description='Screening, job matching, ranking and interview scheduling for one candidate and one job.',
        sub_agents=[
            ScreeningAgent(name='screening_agent', pipeline=run,
                           description='Scores the resume against the job using verified resume evidence.'),
            MatchingAgent(name='matching_agent', pipeline=run,
                          description='Checks the candidate against the hard requirements of the job.'),
            RankingAgent(name='ranking_agent', pipeline=run,
                         description='Ranks the candidate among everyone qualified for the job.'),
            SchedulingAgent(name='scheduling_agent', pipeline=run,
                            description='Proposes an interview time slot.'),
        ],
    )


def run_adk_pipeline(candidate, job) -> PipelineRun:
    run = PipelineRun(candidate=candidate, job=job)
    _text, state = run_agent_sync(
        build_pipeline(run),
        f'Process candidate {candidate.pk} for job {job.pk}',
        state={'candidate_id': candidate.pk, 'job_id': job.pk},
    )
    run.session_state = state
    return run