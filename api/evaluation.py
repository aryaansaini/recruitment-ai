"""Detailed candidate evaluation: what the recruiter reads after a pipeline run.

    overall score, skill match (matched / missing skills), experience match, strengths, weaknesses and a
    final recommendation.

The numbers are computed by plain code from the job and the candidate, so they are always there and always
consistent (also when the AI is unavailable). The AI's own strengths and gaps (from the screening agent)
are used when it produced them; otherwise they are derived from the numbers. No extra model call is made.
"""
import re

from .cv_matching import _in_text
from .rag import experience_score

MAX_POINTS = 4

RECOMMENDATIONS = {
    'strong_hire': 'Strongly recommended',
    'hire': 'Recommended',
    'consider': 'Consider',
    'reject': 'Not recommended',
}


def _skill_names(text) -> list:
    """'Python, Django ,SQL' -> ['Python', 'Django', 'SQL'] (case kept for display, duplicates removed)."""
    seen, names = set(), []
    for part in str(text or '').split(','):
        name = re.sub(r'\s+', ' ', part.strip())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def skill_match(candidate, job) -> dict:
    required = _skill_names(job.required_skills)
    listed = {s.lower() for s in _skill_names(candidate.skills)}
    text = str(candidate.resume_text or '').lower()
    matched = [s for s in required if s.lower() in listed or _in_text(s.lower(), text)]
    missing = [s for s in required if s not in matched]
    percent = round(len(matched) / len(required) * 100) if required else 100
    return {'percent': percent, 'matched': matched, 'missing': missing, 'required_count': len(required)}


def experience_match(candidate, job) -> dict:
    years, minimum = int(candidate.experience or 0), int(job.min_experience or 0)
    meets = years >= minimum
    return {
        'candidate_years': years,
        'required_years': minimum,
        'percent': experience_score(years, minimum),
        'meets': meets,
        'gap_years': max(0, minimum - years),
        'extra_years': max(0, years - minimum),
    }


def _clean(items) -> list:
    out = []
    for item in items or []:
        text = re.sub(r'\s+', ' ', str(item or '')).strip()
        if text and text.lower() not in {o.lower() for o in out}:
            out.append(text[:200])
    return out


def _strengths(skills, experience, ai_items) -> list:
    points = _clean(ai_items)
    if skills['matched'] and len(points) < 2:
        points.append(f"Covers {len(skills['matched'])} of {skills['required_count']} required skills: "
                      + ', '.join(skills['matched'][:6]))
    if experience['meets'] and experience['required_years'] > 0 and len(points) < MAX_POINTS:
        extra = experience['extra_years']
        points.append(f"{experience['candidate_years']} years of experience"
                      + (f" ({extra} more than the {experience['required_years']} required)" if extra else ' (meets the requirement)'))
    return _clean(points)[:MAX_POINTS]


def _weaknesses(skills, experience, ai_items) -> list:
    points = _clean(ai_items)
    if skills['missing'] and len(points) < MAX_POINTS:
        points.append('Required skills not found: ' + ', '.join(skills['missing'][:6]))
    if not experience['meets'] and len(points) < MAX_POINTS:
        gap = experience['gap_years']
        points.append(f"{gap} year{'s' if gap != 1 else ''} below the {experience['required_years']}-year minimum")
    return _clean(points)[:MAX_POINTS]


def recommend(score, skill_percent, meets_experience, status) -> dict:
    """Final call, from the overall score, the skill match, the experience and the pipeline result."""
    if status == 'rejected':
        decision = 'reject'
    elif score >= 80 and skill_percent >= 70 and meets_experience:
        decision = 'strong_hire'
    elif score >= 65 and skill_percent >= 50 and meets_experience:
        decision = 'hire'
    elif score >= 50:
        decision = 'consider'
    else:
        decision = 'reject'

    parts = [f'Overall score {round(score)}/100', f'{skill_percent}% of the required skills matched',
             'experience meets the minimum' if meets_experience else 'experience is below the minimum']
    reason = ', '.join(parts) + '.'
    if status == 'rejected':
        reason += ' The pipeline rejected this candidate for the job.'
    elif decision == 'strong_hire':
        reason += ' A strong fit: proceed to interview.'
    elif decision == 'hire':
        reason += ' A good fit: proceed to interview.'
    elif decision == 'consider':
        reason += ' Review the gaps before deciding.'
    return {'decision': decision, 'label': RECOMMENDATIONS[decision], 'reason': reason}


def build_evaluation(candidate, job) -> dict:
    """Call after the pipeline has set `candidate.score` and the final `candidate.status`."""
    skills = skill_match(candidate, job)
    experience = experience_match(candidate, job)
    score = float(candidate.score or 0)
    return {
        'overall_score': score,
        'skill_match': skills,
        'experience_match': experience,
        'strengths': _strengths(skills, experience, getattr(candidate, 'ai_strengths', [])),
        'weaknesses': _weaknesses(skills, experience, getattr(candidate, 'ai_gaps', [])),
        'recommendation': recommend(score, skills['percent'], experience['meets'], candidate.status),
        'breakdown': getattr(candidate, 'ai_breakdown', None),
        'fallback_used': bool(getattr(candidate, 'fallback_used', False)),
    }
