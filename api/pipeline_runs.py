"""Run the four agents in the background while the dashboard follows their progress.

`POST /api/pipeline-runs/` creates a PipelineExecution and starts the agents in a worker thread; the page then
polls `GET /api/pipeline-runs/<id>/` (see api/tracking.py). The result of a run has the same shape as the answer
of the older `POST /api/run-pipeline/`, plus the detailed evaluation and the interview.
"""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

from . import tracking
from .agents import run_full_pipeline
from .leaderboard import job_leaderboard
from .models import Candidate, Interview, Job, PipelineExecution

logger = logging.getLogger(__name__)


def build_result(candidate, job, slot) -> dict:
    """What the dashboard shows after a run."""
    standing = next((r for r in job_leaderboard(job) if r['candidate_id'] == candidate.id), None)
    interview = Interview.objects.filter(application__candidate=candidate, application__job=job).first()
    return {
        'name': candidate.name,
        'status': candidate.status,
        'score': candidate.score,
        'reason': getattr(candidate, 'ai_reason', ''),
        'fallback_used': getattr(candidate, 'fallback_used', False),
        'breakdown': getattr(candidate, 'ai_breakdown', None),
        'evidence': getattr(candidate, 'ai_evidence', []),
        'retrieval': getattr(candidate, 'ai_retrieval', ''),
        'evaluation': getattr(candidate, 'ai_evaluation', None),
        'interview_slot': slot,
        'interview_id': interview.pk if interview and interview.status in Interview.ACTIVE else None,
        'rank': standing['rank'] if standing else None,
    }


def execute(execution_id):
    """Run one execution to the end (in the calling thread). Never raises: a failure is stored on the run."""
    execution = PipelineExecution.objects.select_related('candidate', 'job').get(pk=execution_id)
    execution.status = PipelineExecution.Status.RUNNING
    execution.started_at = timezone.now()
    execution.save(update_fields=['status', 'started_at'])
    try:
        candidate, slot = run_full_pipeline(execution.candidate_id, execution.job_id, execution_id=execution.pk)
        tracking.finish_execution(execution.pk, result=build_result(candidate, execution.job, slot))
    except Exception as exc:                                  # the run is over: show why instead of spinning forever
        logger.exception('Pipeline execution %s failed', execution_id)
        tracking.finish_execution(execution.pk, error=f'{type(exc).__name__}: {str(exc)[:300]}')


def _thread_main(execution_id):
    close_old_connections()
    try:
        execute(execution_id)
    finally:
        connection.close()                                    # a worker thread must not keep its connection


def start(execution):
    """Start the run in the background (or inline when PIPELINE_RUN_SYNC is on: tests, simple setups)."""
    if getattr(settings, 'PIPELINE_RUN_SYNC', False):
        execute(execution.pk)
        return
    threading.Thread(target=_thread_main, args=(execution.pk,), name=f'pipeline-run-{execution.pk}',
                     daemon=True).start()


def active_execution(owner, candidate_id, job_id):
    """A run of the same candidate and job that is still going (double clicks must not start two)."""
    since = timezone.now() - timedelta(seconds=tracking.STALE_AFTER_SECONDS)
    return (PipelineExecution.objects
            .filter(owner=owner, candidate_id=candidate_id, job_id=job_id, created_at__gte=since,
                    status__in=[PipelineExecution.Status.QUEUED, PipelineExecution.Status.RUNNING])
            .first())


def create_and_start(owner, candidate: Candidate, job: Job):
    """Returns (execution, started). started is False when an identical run was already going."""
    existing = active_execution(owner, candidate.pk, job.pk)
    if existing:
        return existing, False
    execution = tracking.create_execution(owner, candidate, job)
    start(execution)
    return PipelineExecution.objects.select_related('candidate', 'job').get(pk=execution.pk), True
