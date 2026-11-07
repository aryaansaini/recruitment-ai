"""ATS MCP server: read and update candidate records of ONE recruiter (MCP_OWNER_ID).

Tools
    update_candidate_status(candidate_id, status, score?, interview_date?)
    get_candidate(candidate_id)
    list_candidates(status?, limit?)

Run:  MCP_OWNER_ID=3 python -m api.mcp_servers.ats_server
"""
import math
from datetime import timezone as dt_timezone
from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP

from ._common import run_stdio


# -- plain functions (they need Django, so FastMCP tools call them in a worker thread) ----------------------
def _find(owner_id: int, candidate_id: int):
    from api.models import Candidate
    candidate = Candidate.objects.filter(pk=candidate_id, owner_id=owner_id).first()
    if candidate is None:
        raise ValueError('Candidate not found.')
    return candidate


def _check_status(status):
    from api.models import Candidate
    if status not in Candidate.Status.values:
        raise ValueError('status must be one of: ' + ', '.join(Candidate.Status.values))
    return status


def _iso(value):
    return value.isoformat() if value else None


def _summary(c) -> dict:
    return {'id': c.pk, 'name': c.name, 'status': c.status, 'score': c.score, 'interview_date': _iso(c.interview_date)}


def update_status(owner_id: int, candidate_id: int, status: str, score=None, interview_date=None) -> str:
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    _check_status(status)
    if score is not None and not (isinstance(score, (int, float)) and not math.isnan(score) and 0 <= score <= 100):
        raise ValueError('score must be a number from 0 to 100.')
    when = None
    if interview_date is not None:
        when = parse_datetime(interview_date)
        if when is None:
            raise ValueError('interview_date must be an ISO 8601 date and time, e.g. 2026-11-09T10:00:00+00:00.')
        if timezone.is_naive(when):
            when = timezone.make_aware(when, dt_timezone.utc)

    candidate = _find(owner_id, candidate_id)
    candidate.status = status
    fields = ['status']
    if score is not None:
        candidate.score = float(score)
        fields.append('score')
    if when is not None:
        candidate.interview_date = when
        fields.append('interview_date')
    candidate.save(update_fields=fields)
    return f"ATS Updated: {candidate.name} -> {status}"


def get_record(owner_id: int, candidate_id: int) -> dict:
    c = _find(owner_id, candidate_id)
    return {**_summary(c), 'email': c.email, 'phone': c.phone, 'skills': c.skills, 'experience': c.experience,
            'resume_characters': len(c.resume_text), 'created_at': _iso(c.created_at)}


def list_records(owner_id: int, status=None, limit: int = 20) -> list:
    from api.models import Candidate
    qs = Candidate.objects.filter(owner_id=owner_id)
    if status:
        qs = qs.filter(status=_check_status(status))
    limit = max(1, min(int(limit), 50))
    return [_summary(c) for c in qs.order_by('-score', 'id')[:limit]]


# -- the server -------------------------------------------------------------------------------------------------
def create_server(owner_id: int) -> FastMCP:
    server = FastMCP(
        'recruit-ats',
        instructions='Applicant tracking system of one recruiter. Candidate ids come from list_candidates.')

    @server.tool()
    async def update_candidate_status(candidate_id: int, status: str, score: float | None = None,
                                      interview_date: str | None = None) -> str:
        """Set a candidate's pipeline status, and optionally their score (0 to 100) and interview time
        (ISO 8601). Status is one of: pending, shortlisted, matched, ranked, interview_scheduled, rejected."""
        return await sync_to_async(update_status)(owner_id, candidate_id, status, score, interview_date)

    @server.tool()
    async def get_candidate(candidate_id: int) -> dict[str, Any]:
        """Get one candidate's record (contact details, skills, experience, score, status, interview time).
        The resume text itself is not returned."""
        return await sync_to_async(get_record)(owner_id, candidate_id)

    @server.tool()
    async def list_candidates(status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List candidates, best score first, optionally only those with one status (at most 50)."""
        return await sync_to_async(list_records)(owner_id, status, limit)

    return server


def main():
    run_stdio(create_server, needs_owner=True)


if __name__ == '__main__':
    main()
