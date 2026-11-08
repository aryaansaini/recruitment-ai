import logging
from datetime import timedelta

from django.utils import timezone

from api import rag
from api.llm import LLMError, MatchResult, generate_structured
from api.ranking import QUALIFIED_STATUSES, competition_rank
from api.tracking import tracked

logger = logging.getLogger(__name__)

SHORTLIST_THRESHOLD = 50


# ============================================
# The three MCP servers (api/mcp_servers/) and what runs when they are not used
# ============================================
def _via_mcp(server, tool, arguments, owner_id=None):
    """(True, value) from an MCP server, or (False, None) when MCP is switched off (USE_MCP) or the server
    cannot be used, so the caller does the same work in-process. A tool that refuses the request raises."""
    from django.conf import settings
    if not getattr(settings, 'USE_MCP', False):
        return False, None
    from api import mcp_client
    try:
        return True, mcp_client.call_tool(server, tool, arguments, owner_id=owner_id)
    except mcp_client.McpToolError:
        raise
    except mcp_client.McpError as exc:
        logger.warning('MCP server %s unavailable, doing %s in-process (%s)', server, tool, exc)
        return False, None


# MCP Server 3: Resume Parser
def resume_parser_mcp(resume_text):
    used, parsed = _via_mcp('resume_parser', 'analyze_resume', {'text': resume_text})
    if used:
        return parsed
    from api.resume_parser import keyword_summary
    return keyword_summary(resume_text)


# MCP Server 2: Calendar
def calendar_mcp(candidate_name, owner_id=None):
    if owner_id is not None:
        used, slot = _via_mcp('calendar', 'propose_interview_slot', {'candidate_name': candidate_name}, owner_id)
        if used:
            from django.utils.dateparse import parse_datetime
            return {**slot, 'datetime': parse_datetime(slot['datetime'])}
    interview_time = (timezone.now() + timedelta(days=3)).replace(second=0, microsecond=0)
    return {
        'candidate': candidate_name,
        'slot': interview_time.strftime('%Y-%m-%d %H:%M'),
        'datetime': interview_time,
        'platform': 'Google Meet',
        'link': 'meet.google.com/recruitment-' + candidate_name.lower().replace(' ', '-')
    }


# MCP Server 1: ATS
def ats_mcp_update(candidate, status):
    """Record the candidate's status together with the score and interview time the pipeline has set."""
    if candidate.owner_id is not None:
        arguments = {'candidate_id': candidate.pk, 'status': status, 'score': candidate.score}
        if candidate.interview_date:
            arguments['interview_date'] = candidate.interview_date.isoformat()
        used, message = _via_mcp('ats', 'update_candidate_status', arguments, candidate.owner_id)
        if used:
            candidate.status = status          # the server has saved it; keep this object in step
            return message
    candidate.status = status
    candidate.save()
    return f"ATS Updated: {candidate.name} -> {status}"


# ============================================
# Rule-based fallbacks (used ONLY when the AI call fails, and always flagged)
# ============================================
def _rule_based_score(candidate):
    parsed = resume_parser_mcp(candidate.resume_text)
    score = len([s for s in candidate.skills.split(',') if s.strip()]) * 10
    score += candidate.experience * 5
    score += parsed['keyword_count'] * 8
    return float(min(100, score))


def _rule_based_match(candidate, job):
    candidate_skills = {s.strip().lower() for s in candidate.skills.split(',') if s.strip()}
    required_skills = {s.strip().lower() for s in job.required_skills.split(',') if s.strip()}
    matched = candidate_skills & required_skills
    percent = (len(matched) / len(required_skills)) * 100 if required_skills else 0
    return percent >= 50 and candidate.experience >= job.min_experience, round(percent)


def _db_emit(candidate):
    """Default activity-log writer: one AgentLog row per call. The ADK pipelines pass their own `emit`
    instead, so that their ADK callbacks write the log."""
    from api.models import AgentLog

    def emit(from_agent, to_agent, message):
        AgentLog.objects.create(candidate=candidate, from_agent=from_agent, to_agent=to_agent, message=message)
    return emit


def _fallback_note(candidate):
    return ' (rule-based fallback: AI unavailable)' if getattr(candidate, 'fallback_used', False) else ''


# ============================================
# AGENT 1: Resume Screening Agent (RAG: scores from the resume parts that match the job)
# ============================================
@tracked('screening', lambda c, r: f'Score {c.score}/100, {c.status}{_fallback_note(c)}')
def resume_screening_agent(candidate, job, extra_chunks=None, emit=None):
    """`extra_chunks`: resume parts an LLM agent searched for itself; they are added to the evidence."""
    emit = emit or _db_emit(candidate)
    print(f"[Agent 1] Resume Screening: {candidate.name} for {job.title}")

    evidence = rag.retrieve_evidence(candidate, job)      # never raises; may fall back (see card['retrieval'])
    if extra_chunks:
        evidence = rag.merge_chunks(evidence, extra_chunks)
    try:
        card = rag.score_candidate(candidate, job, llm=generate_structured, evidence=evidence)
        score, reason, fallback = card['overall'], card['summary'], False
    except LLMError as exc:
        logger.warning('Screening: AI failed, using rule-based fallback (%s)', exc)
        card = None
        score = _rule_based_score(candidate)
        reason = 'AI unavailable: score from rule-based fallback (skills, experience, resume keywords).'
        fallback = True

    candidate.score = score
    candidate.ai_reason = reason          # transient: carried to the Application at the end
    candidate.fallback_used = fallback
    candidate.ai_breakdown = card['breakdown'] if card else None
    candidate.ai_evidence = card['evidence'] if card else []
    candidate.ai_retrieval = card['retrieval'] if card else evidence['mode']
    candidate.ai_strengths = card.get('strengths', []) if card else []       # used by the detailed evaluation
    candidate.ai_gaps = card.get('gaps', []) if card else []

    if score >= SHORTLIST_THRESHOLD:
        ats_mcp_update(candidate, 'shortlisted')
        result_status = 'shortlisted'
    else:
        ats_mcp_update(candidate, 'rejected')
        result_status = 'rejected'

    cited = f" [{len(candidate.ai_evidence)} verified quotes, retrieval={candidate.ai_retrieval}]"
    emit('Resume Screening Agent', 'Job Matching Agent',
         f"{'FALLBACK ' if fallback else 'AI '}Score: {score}, Status: {result_status}. {reason}{cited}")

    print(f"[Agent 1] Done. Score={score}, Status={result_status}, fallback={fallback}")
    return candidate


# ============================================
# AGENT 2: Job Matching Agent (Gemini, structured output)
# ============================================
@tracked('matching', lambda c, r: ('Matched' if r else 'Not matched') + _fallback_note(c))
def job_matching_agent(candidate, job, decision=None, emit=None):
    """`decision`: (is_match, percent, reason) already made by an LLM agent. Without it, Gemini is asked
    here, with the rule-based fallback."""
    emit = emit or _db_emit(candidate)
    print(f"[Agent 2] Job Matching: {candidate.name} <-> {job.title}")

    if candidate.status == 'rejected':
        return False

    prompt = f"""Decide whether this candidate matches the job's hard requirements.

Job title: {job.title}
Required skills: {job.required_skills}
Minimum experience: {job.min_experience} years

Candidate skills: {candidate.skills}
Candidate experience: {candidate.experience} years"""

    if decision is not None:
        is_match, percent, match_reason = decision
    else:
        try:
            result = generate_structured(prompt, MatchResult)
            is_match, percent, match_reason = result.is_match, result.match_percent, result.reason
        except LLMError as exc:
            logger.warning('Matching: AI failed, using rule-based fallback (%s)', exc)
            is_match, percent = _rule_based_match(candidate, job)
            match_reason = 'AI unavailable: rule-based skill overlap.'
            candidate.fallback_used = True

    candidate.ai_reason = f"{getattr(candidate, 'ai_reason', '')} Match {percent}%: {match_reason}".strip()

    emit('Job Matching Agent', 'Candidate Ranking Agent',
         f"{'Matched' if is_match else 'Not Matched'} ({percent}%). {match_reason}")

    if is_match:
        ats_mcp_update(candidate, 'matched')
        return True

    ats_mcp_update(candidate, 'rejected')
    return False


# ============================================
# AGENT 3: Candidate Ranking Agent
# ============================================

@tracked('ranking', lambda c, r: f"Rank #{getattr(c, 'pipeline_rank', '?')}")
def candidate_ranking_agent(candidates, job=None, emit=None):
    """Rank candidates. With `job`, each one is ranked against everyone already
    qualified for that job (stored applications), not only against this batch."""
    from api.models import Application
    print(f"[Agent 3] Ranking {len(candidates)} candidates")

    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)

    peer_scores = []
    if job is not None:
        peer_scores = list(
            Application.objects.filter(job=job, status__in=QUALIFIED_STATUSES)
            .exclude(candidate_id__in=[c.pk for c in ranked])
            .values_list('score', flat=True)
        )
    all_scores = [c.score for c in ranked] + peer_scores

    for i, candidate in enumerate(ranked):
        rank = competition_rank(candidate.score, all_scores) if job is not None else i + 1
        message = f"Rank #{rank}, Score: {candidate.score}"
        if job is not None:
            message += f" (of {len(all_scores)} qualified for {job.title})"
        ats_mcp_update(candidate, 'ranked')
        candidate.pipeline_rank = rank                    # transient, read by the ADK ranking tool
        (emit or _db_emit(candidate))('Candidate Ranking Agent', 'Interview Coordination Agent', message)
        print(f"[Agent 3] Rank #{rank}: {candidate.name}")

    return ranked

# ============================================
# AGENT 4: Interview Coordination Agent
# ============================================
@tracked('scheduling', lambda c, r: f"Interview {r['slot']} via {r['platform']}")
def interview_coordination_agent(candidate, emit=None):
    emit = emit or _db_emit(candidate)
    print(f"[Agent 4] Scheduling interview: {candidate.name}")

    slot = calendar_mcp(candidate.name, owner_id=candidate.owner_id)

    candidate.interview_date = slot['datetime']
    ats_mcp_update(candidate, 'interview_scheduled')

    emit('Interview Coordination Agent', 'Recruiter Dashboard',
         f"Interview on {slot['slot']} via {slot['platform']}")

    print(f"[Agent 4] Done. Slot: {slot['slot']}")
    return {k: v for k, v in slot.items() if k != 'datetime'}   # JSON-safe


# ============================================
# MAIN PIPELINE
# ============================================
def run_full_pipeline(candidate_id, job_id, execution_id=None):
    """`execution_id`: the PipelineExecution to report live progress to (see api/tracking.py)."""
    from django.conf import settings
    from api import interviews
    from api.evaluation import build_evaluation
    from api.models import Application, Candidate, Job

    candidate = Candidate.objects.get(id=candidate_id)
    job = Job.objects.get(id=job_id)
    candidate.pipeline_exec_id = execution_id          # transient: the agents read it to report their progress

    # Candidate status is shared across applications. Reset the working state before
    # evaluating another job so a rejection for one role cannot block the next role.
    candidate.status = Candidate.Status.PENDING
    candidate.interview_date = None
    candidate.save(update_fields=['status', 'interview_date'])

    print("===== PIPELINE START =====")

    if getattr(settings, 'PIPELINE_USE_ADK', True):
        # the four agents run as Google ADK agents inside a SequentialAgent
        if getattr(settings, 'PIPELINE_AGENT_MODE', 'deterministic') == 'llm' and settings.GEMINI_API_KEY:
            from api.adk_pipeline_llm import run_llm_pipeline      # LlmAgents that call tools themselves
            outcome = run_llm_pipeline(candidate, job)
        else:
            from api.adk_pipeline import run_adk_pipeline          # fixed agents, no extra model calls
            outcome = run_adk_pipeline(candidate, job)
        candidate, slot = outcome.candidate, outcome.slot
    else:
        candidate = resume_screening_agent(candidate, job)
        is_match = job_matching_agent(candidate, job)
        if is_match:
            ranked = candidate_ranking_agent([candidate], job=job)
            slot = interview_coordination_agent(ranked[0])
        else:
            slot = None

    # Detailed evaluation: skill match, missing skills, experience match, strengths, weaknesses, recommendation
    candidate.ai_evaluation = build_evaluation(candidate, job)

    # Per-job record (a candidate can apply to many jobs)
    application, _created = Application.objects.update_or_create(
        candidate=candidate,
        job=job,
        defaults={
            'score': candidate.score,
            'status': candidate.status,
            'interview_date': candidate.interview_date,
            'reason': getattr(candidate, 'ai_reason', ''),
            'fallback_used': getattr(candidate, 'fallback_used', False),
            'breakdown': getattr(candidate, 'ai_breakdown', None),
            'evidence': getattr(candidate, 'ai_evidence', []),
            'evaluation': candidate.ai_evaluation,
        },
    )

    # The proposed time becomes a managed Interview (calendar, reschedule, cancel, e-mails)
    interview = interviews.sync_from_pipeline(application, slot)
    if interview is not None and interview.scheduled_at != candidate.interview_date:
        candidate.interview_date = interview.scheduled_at         # the slot had to move: keep the result in step
        if slot:
            slot = {**slot, 'slot': interview.scheduled_at.strftime('%Y-%m-%d %H:%M')}

    print("===== PIPELINE END =====")
    return candidate, slot