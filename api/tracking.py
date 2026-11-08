"""Live tracking of the four agents.

One `PipelineExecution` row per run and one `AgentStep` row per agent. The agent functions in
`api/agents.py` are wrapped with `@tracked(...)`: they report "running", "completed" or "failed" (with the
time it took and the error text) to these rows, and the dashboard polls `GET /api/pipeline-runs/<id>/`.

Tracking is optional and must never break a run: a candidate without `pipeline_exec_id` (every direct call
in older code and tests) is simply not tracked, and a database error while tracking is logged and ignored.
"""
import functools
import logging

from django.utils import timezone

from .models import AgentStep, PipelineExecution

logger = logging.getLogger(__name__)

STEPS = (
    ('screening', 'Resume Screening Agent'),
    ('matching', 'Job Matching Agent'),
    ('ranking', 'Candidate Ranking Agent'),
    ('scheduling', 'Interview Coordination Agent'),
)
STALE_AFTER_SECONDS = 10 * 60          # a run that is still "running" after this long was lost (server restart)


def create_execution(owner, candidate, job) -> PipelineExecution:
    execution = PipelineExecution.objects.create(owner=owner, candidate=candidate, job=job)
    AgentStep.objects.bulk_create([
        AgentStep(execution=execution, key=key, label=label, order=i)
        for i, (key, label) in enumerate(STEPS)
    ])
    return execution


def _step(execution_id, key):
    return AgentStep.objects.filter(execution_id=execution_id, key=key).first()


def start_step(execution_id, key):
    """Mark a step as running. Calling it again keeps the first start time (the LLM agents start the step
    before they call their tool, so the time the model needs is part of the duration)."""
    step = _step(execution_id, key)
    if step is None or step.status in (AgentStep.Status.COMPLETED, AgentStep.Status.FAILED, AgentStep.Status.SKIPPED):
        return
    if step.started_at is None:
        step.started_at = timezone.now()
    step.status = AgentStep.Status.RUNNING
    step.save(update_fields=['status', 'started_at'])


def finish_step(execution_id, key, status, message='', error=''):
    step = _step(execution_id, key)
    if step is None:
        return
    now = timezone.now()
    if step.started_at is None and status != AgentStep.Status.SKIPPED:
        step.started_at = now
    step.status = status
    step.finished_at = now
    step.duration_ms = (int((now - step.started_at).total_seconds() * 1000)
                        if step.started_at and status != AgentStep.Status.SKIPPED else None)
    step.message = message[:1000]
    step.error = error[:1000]
    step.save(update_fields=['status', 'finished_at', 'duration_ms', 'message', 'error', 'started_at'])


def mark_running(candidate, key):
    """Used by the LLM agents' callbacks: the agent has started, even if its tool has not been called yet."""
    exec_id = getattr(candidate, 'pipeline_exec_id', None)
    if exec_id:
        try:
            start_step(exec_id, key)
        except Exception:                                   # tracking must never break a run
            logger.warning('Could not track the start of %s', key, exc_info=True)


def mark_skipped(candidate, key, message):
    exec_id = getattr(candidate, 'pipeline_exec_id', None)
    if exec_id:
        try:
            finish_step(exec_id, key, AgentStep.Status.SKIPPED, message=message)
        except Exception:
            logger.warning('Could not track the skip of %s', key, exc_info=True)


def tracked(key, summarize):
    """Decorator for an agent function whose first argument is the candidate (or, for the ranking agent,
    a list of candidates). `summarize(candidate, result)` returns the one-line message shown in the dashboard."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            subject = args[0] if args else kwargs.get('candidate', kwargs.get('candidates'))
            candidate = subject[0] if isinstance(subject, (list, tuple)) and subject else subject
            exec_id = getattr(candidate, 'pipeline_exec_id', None)
            if not exec_id:
                return fn(*args, **kwargs)
            if key == 'matching' and getattr(candidate, 'status', '') == 'rejected':
                mark_skipped(candidate, key, 'Skipped: the candidate was rejected at screening.')
                return fn(*args, **kwargs)
            try:
                start_step(exec_id, key)
            except Exception:
                logger.warning('Could not track the start of %s', key, exc_info=True)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                try:
                    finish_step(exec_id, key, AgentStep.Status.FAILED, error=f'{type(exc).__name__}: {exc}')
                except Exception:
                    logger.warning('Could not track the failure of %s', key, exc_info=True)
                raise
            try:
                finish_step(exec_id, key, AgentStep.Status.COMPLETED, message=summarize(candidate, result))
            except Exception:
                logger.warning('Could not track the end of %s', key, exc_info=True)
            return result
        return wrapper
    return decorator


def finish_execution(execution_id, result=None, error=''):
    """Close a run. Steps that never ran are marked skipped (the candidate did not qualify) or, after an
    error, as not run."""
    execution = PipelineExecution.objects.filter(pk=execution_id).first()
    if execution is None:
        return None
    pending = execution.steps.filter(status__in=[AgentStep.Status.PENDING, AgentStep.Status.RUNNING])
    if error:
        for step in pending:
            if step.status == AgentStep.Status.RUNNING:
                finish_step(execution_id, step.key, AgentStep.Status.FAILED, error=error)
            else:
                finish_step(execution_id, step.key, AgentStep.Status.SKIPPED, message='Not run: an earlier step failed.')
    else:
        for step in pending:
            finish_step(execution_id, step.key, AgentStep.Status.SKIPPED, message='Skipped: the candidate did not qualify.')
    execution.status = PipelineExecution.Status.FAILED if error else PipelineExecution.Status.COMPLETED
    execution.error = error[:2000]
    execution.result = result
    execution.finished_at = timezone.now()
    execution.save(update_fields=['status', 'error', 'result', 'finished_at'])
    return execution


def expire_if_stale(execution):
    """A run that has been active for too long lost its worker (e.g. the server restarted): fail it."""
    if execution.status in (PipelineExecution.Status.QUEUED, PipelineExecution.Status.RUNNING):
        age = (timezone.now() - execution.created_at).total_seconds()
        if age > STALE_AFTER_SECONDS:
            return finish_execution(execution.pk, error='The run did not finish (the server may have restarted). '
                                                        'Please run it again.')
    return execution


def serialize(execution) -> dict:
    steps = [{
        'key': s.key, 'label': s.label, 'order': s.order, 'status': s.status,
        'started_at': s.started_at, 'finished_at': s.finished_at, 'duration_ms': s.duration_ms,
        'message': s.message, 'error': s.error,
    } for s in execution.steps.all()]
    total_ms = None
    if execution.started_at and execution.finished_at:
        total_ms = int((execution.finished_at - execution.started_at).total_seconds() * 1000)
    return {
        'id': execution.pk, 'status': execution.status,
        'candidate_id': execution.candidate_id, 'candidate_name': execution.candidate.name,
        'job_id': execution.job_id, 'job_title': execution.job.title,
        'created_at': execution.created_at, 'started_at': execution.started_at,
        'finished_at': execution.finished_at, 'duration_ms': total_ms,
        'error': execution.error, 'result': execution.result, 'steps': steps,
    }
