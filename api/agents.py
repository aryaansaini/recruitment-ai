import logging
from datetime import timedelta

from django.utils import timezone

from api import rag
from api.llm import LLMError, MatchResult, generate_structured
from api.ranking import QUALIFIED_STATUSES, competition_rank

logger = logging.getLogger(__name__)

SHORTLIST_THRESHOLD = 50


# ============================================
# MCP Server 3: Resume Parser MCP
# ============================================
def resume_parser_mcp(resume_text):
    words = resume_text.lower().split()
    tech_keywords = ['python', 'django', 'sql', 'react', 'javascript',
                     'java', 'html', 'css', 'machine learning', 'ai']
    found = [k for k in tech_keywords if k in words]
    return {
        'word_count': len(words),
        'keywords_found': found,
        'keyword_count': len(found)
    }


# ============================================
# MCP Server 2: Calendar MCP
# ============================================
def calendar_mcp(candidate_name):
    interview_time = (timezone.now() + timedelta(days=3)).replace(second=0, microsecond=0)
    return {
        'candidate': candidate_name,
        'slot': interview_time.strftime('%Y-%m-%d %H:%M'),
        'datetime': interview_time,
        'platform': 'Google Meet',
        'link': 'meet.google.com/recruitment-' + candidate_name.lower().replace(' ', '-')
    }


# ============================================
# MCP Server 1: ATS MCP
# ============================================
def ats_mcp_update(candidate, status):
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


# ============================================
# AGENT 1: Resume Screening Agent (RAG: scores from the resume parts that match the job)
# ============================================
def resume_screening_agent(candidate, job):
    from api.models import AgentLog
    print(f"[Agent 1] Resume Screening: {candidate.name} for {job.title}")

    evidence = rag.retrieve_evidence(candidate, job)      # never raises; may fall back (see card['retrieval'])
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

    if score >= SHORTLIST_THRESHOLD:
        ats_mcp_update(candidate, 'shortlisted')
        result_status = 'shortlisted'
    else:
        ats_mcp_update(candidate, 'rejected')
        result_status = 'rejected'

    cited = f" [{len(candidate.ai_evidence)} verified quotes, retrieval={candidate.ai_retrieval}]"
    AgentLog.objects.create(
        candidate=candidate,
        from_agent='Resume Screening Agent',
        to_agent='Job Matching Agent',
        message=f"{'FALLBACK ' if fallback else 'AI '}Score: {score}, Status: {result_status}. {reason}{cited}"
    )

    print(f"[Agent 1] Done. Score={score}, Status={result_status}, fallback={fallback}")
    return candidate


# ============================================
# AGENT 2: Job Matching Agent (Gemini, structured output)
# ============================================
def job_matching_agent(candidate, job):
    from api.models import AgentLog
    print(f"[Agent 2] Job Matching: {candidate.name} <-> {job.title}")

    if candidate.status == 'rejected':
        return False

    prompt = f"""Decide whether this candidate matches the job's hard requirements.

Job title: {job.title}
Required skills: {job.required_skills}
Minimum experience: {job.min_experience} years

Candidate skills: {candidate.skills}
Candidate experience: {candidate.experience} years"""

    try:
        result = generate_structured(prompt, MatchResult)
        is_match, percent, match_reason = result.is_match, result.match_percent, result.reason
    except LLMError as exc:
        logger.warning('Matching: AI failed, using rule-based fallback (%s)', exc)
        is_match, percent = _rule_based_match(candidate, job)
        match_reason = 'AI unavailable: rule-based skill overlap.'
        candidate.fallback_used = True

    candidate.ai_reason = f"{getattr(candidate, 'ai_reason', '')} Match {percent}%: {match_reason}".strip()

    AgentLog.objects.create(
        candidate=candidate,
        from_agent='Job Matching Agent',
        to_agent='Candidate Ranking Agent',
        message=f"{'Matched' if is_match else 'Not Matched'} ({percent}%). {match_reason}"
    )

    if is_match:
        ats_mcp_update(candidate, 'matched')
        return True

    ats_mcp_update(candidate, 'rejected')
    return False


# ============================================
# AGENT 3: Candidate Ranking Agent
# ============================================

def candidate_ranking_agent(candidates, job=None):
    """Rank candidates. With `job`, each one is ranked against everyone already
    qualified for that job (stored applications), not only against this batch."""
    from api.models import AgentLog, Application
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
        AgentLog.objects.create(
            candidate=candidate,
            from_agent='Candidate Ranking Agent',
            to_agent='Interview Coordination Agent',
            message=message,
        )
        print(f"[Agent 3] Rank #{rank}: {candidate.name}")

    return ranked

# ============================================
# AGENT 4: Interview Coordination Agent
# ============================================
def interview_coordination_agent(candidate):
    from api.models import AgentLog
    print(f"[Agent 4] Scheduling interview: {candidate.name}")

    slot = calendar_mcp(candidate.name)

    candidate.interview_date = slot['datetime']
    ats_mcp_update(candidate, 'interview_scheduled')

    AgentLog.objects.create(
        candidate=candidate,
        from_agent='Interview Coordination Agent',
        to_agent='Recruiter Dashboard',
        message=f"Interview on {slot['slot']} via {slot['platform']}"
    )

    print(f"[Agent 4] Done. Slot: {slot['slot']}")
    return {k: v for k, v in slot.items() if k != 'datetime'}   # JSON-safe


# ============================================
# MAIN PIPELINE
# ============================================
def run_full_pipeline(candidate_id, job_id):
    from django.conf import settings
    from api.models import Application, Candidate, Job

    candidate = Candidate.objects.get(id=candidate_id)
    job = Job.objects.get(id=job_id)

    print("===== PIPELINE START =====")

    if getattr(settings, 'PIPELINE_USE_ADK', True):
        # the four agents run as Google ADK agents inside a SequentialAgent
        from api.adk_pipeline import run_adk_pipeline
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

    # Per-job record (a candidate can apply to many jobs)
    Application.objects.update_or_create(
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
        },
    )

    print("===== PIPELINE END =====")
    return candidate, slot