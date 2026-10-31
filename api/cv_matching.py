
import re

ELIGIBLE_SKILL_PERCENT = 50      # same threshold as the pipeline's rule-based matcher
PARTIAL_SKILL_PERCENT = 30

_VERDICT_ORDER = {'eligible': 0, 'partial': 1, 'not_a_match': 2}


def split_skills(text):
    """'Python, Django ,SQL' -> ['python', 'django', 'sql']"""
    items = (re.sub(r'\s+', ' ', part.strip().lower()) for part in str(text or '').split(','))
    return [s for s in items if s]


def _in_text(skill, text_lower):
    """Whole-word match, so 'java' does not match 'javascript' and 'c' does not match 'c++'."""
    pattern = r'(?<![a-z0-9+#])' + re.escape(skill) + r'(?![a-z0-9+#])'
    return re.search(pattern, text_lower) is not None


def match_job(job, cv_skills, text_lower, experience):
    required = split_skills(job['required_skills'])
    matched = [s for s in required if s in cv_skills or _in_text(s, text_lower)]
    missing = [s for s in required if s not in matched]
    skill_percent = round(len(matched) / len(required) * 100) if required else 100

    min_experience = int(job['min_experience'] or 0)
    experience_ok = experience >= min_experience
    experience_score = 100 if experience_ok else round(experience / min_experience * 100)

    if skill_percent >= ELIGIBLE_SKILL_PERCENT and experience_ok:
        verdict = 'eligible'
    elif skill_percent >= PARTIAL_SKILL_PERCENT:
        verdict = 'partial'
    else:
        verdict = 'not_a_match'

    return {
        'id': job['id'],
        'title': job['title'],
        'score': round(0.7 * skill_percent + 0.3 * experience_score),
        'verdict': verdict,
        'skill_percent': skill_percent,
        'matched_skills': matched,
        'missing_skills': missing,
        'min_experience': min_experience,
        'experience_ok': experience_ok,
    }


def rank_jobs(jobs, skills, experience, resume_text=''):
    """Best fit first: eligible jobs, then partial matches, then the rest."""
    cv_skills = {re.sub(r'\s+', ' ', str(s).strip().lower()) for s in skills if str(s).strip()}
    text_lower = str(resume_text or '').lower()
    results = [match_job(job, cv_skills, text_lower, int(experience)) for job in jobs]
    return sorted(results, key=lambda r: (_VERDICT_ORDER[r['verdict']], -r['score'], r['title'].lower()))