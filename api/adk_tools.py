"""
Google ADK tools (plain functions the agents can call).

ADK runs inside an event loop, Django's ORM must not run there, so each tool is an
`async def` that hands the real work to a thread via sync_to_async.
Tools return a dict with "status": "success" | "error" (never raise): the agent can read the error.
"""
from asgiref.sync import sync_to_async

from . import rag
from .llm import LLMError
from .models import Candidate, Job


def _search_impl(candidate_id, query, top_k):
    candidate = Candidate.objects.filter(pk=candidate_id).first()
    if candidate is None:
        return {'status': 'error', 'message': f'Candidate {candidate_id} not found.'}
    query = (query or '').strip()
    if len(query) < 2:
        return {'status': 'error', 'message': 'query must be at least 2 characters.'}
    top_k = max(1, min(int(top_k), 10))
    mode, chunks = rag.semantic_chunks(candidate, query, top_k)
    return {'status': 'success', 'retrieval': mode, 'chunks': chunks}


def _score_impl(candidate_id, job_id):
    candidate = Candidate.objects.filter(pk=candidate_id).first()
    job = Job.objects.filter(pk=job_id).first()
    if candidate is None or job is None:
        return {'status': 'error', 'message': 'Candidate or job not found.'}
    try:
        card = rag.score_candidate(candidate, job)
    except LLMError as exc:
        return {'status': 'error', 'message': f'AI scoring failed: {exc}'}
    return {'status': 'success', **card}


async def search_resume_chunks(candidate_id: int, query: str, top_k: int = 5) -> dict:
    """Find the parts of ONE candidate's resume that best match a query.

    Args:
        candidate_id: Database id of the candidate.
        query: What to look for, e.g. "REST API experience with Django".
        top_k: How many resume chunks to return (1-10).

    Returns:
        A dict with status, the retrieval mode, and a list of chunks
        (chunk_id, section, text, similarity).
    """
    return await sync_to_async(_search_impl)(candidate_id, query, top_k)


async def score_candidate(candidate_id: int, job_id: int) -> dict:
    """Score how well a candidate fits a job, using evidence from their resume.

    Args:
        candidate_id: Database id of the candidate.
        job_id: Database id of the job.

    Returns:
        A dict with status, overall score (0-100), a breakdown (skills, experience, education),
        a summary, strengths, gaps and verified resume quotes as evidence.
    """
    return await sync_to_async(_score_impl)(candidate_id, job_id)