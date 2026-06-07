
import google.generativeai as genai
from datetime import datetime, timedelta


import os
from dotenv import load_dotenv
load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
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
    interview_time = datetime.now() + timedelta(days=3)
    return {
        'candidate': candidate_name,
        'slot': interview_time.strftime('%Y-%m-%d %H:%M'),
        'platform': 'Google Meet',
        'link': 'meet.google.com/recruitment-' + candidate_name.lower().replace(' ', '-')
    }

# ============================================
# MCP Server 1: ATS MCP
# ============================================
def ats_mcp_update(candidate, status):
    candidate.status = status
    candidate.save()
    return f"ATS Updated: {candidate.name} → {status}"

# ============================================
# AGENT 1: Resume Screening Agent (Gemini AI)
# ============================================
def resume_screening_agent(candidate):
    from api.models import AgentLog
    print(f"[Agent 1] Resume Screening: {candidate.name}")

    # Gemini AI se resume analyze karwao
    prompt = f"""
    Analyze this resume and give a score from 0 to 100.
    
    Candidate Name: {candidate.name}
    Skills: {candidate.skills}
    Experience: {candidate.experience} years
    Resume: {candidate.resume_text}
    
    Reply in this exact format only:
    SCORE: <number>
    REASON: <one line reason>
    """

    try:
        response = model.generate_content(prompt)
        text = response.text
        lines = text.strip().split('\n')
        score_line = [l for l in lines if 'SCORE:' in l][0]
        score = float(score_line.replace('SCORE:', '').strip())
    except:
        # Fallback to rule-based if API fails
        parsed = resume_parser_mcp(candidate.resume_text)
        score = len(candidate.skills.split(',')) * 10
        score += candidate.experience * 5
        score += parsed['keyword_count'] * 8

    candidate.score = score

    if score >= 50:
        ats_mcp_update(candidate, 'shortlisted')
        result = 'shortlisted'
    else:
        ats_mcp_update(candidate, 'rejected')
        result = 'rejected'

    AgentLog.objects.create(
        candidate=candidate,
        from_agent='Resume Screening Agent',
        to_agent='Job Matching Agent',
        message=f"AI Score: {score}, Status: {result}"
    )

    print(f"[Agent 1] Done. Score={score}, Status={result}")
    return candidate

# ============================================
# AGENT 2: Job Matching Agent (Gemini AI)
# ============================================
def job_matching_agent(candidate, job):
    from api.models import AgentLog
    print(f"[Agent 2] Job Matching: {candidate.name} ↔ {job.title}")

    if candidate.status == 'rejected':
        return False

    prompt = f"""
    Check if this candidate matches the job.
    
    Job Title: {job.title}
    Required Skills: {job.required_skills}
    Min Experience: {job.min_experience} years
    
    Candidate Skills: {candidate.skills}
    Candidate Experience: {candidate.experience} years
    
    Reply in this exact format only:
    MATCH: YES or NO
    PERCENT: <number>
    """

    try:
        response = model.generate_content(prompt)
        text = response.text
        match_line = [l for l in text.split('\n') if 'MATCH:' in l][0]
        is_match = 'YES' in match_line.upper()
    except:
        # Fallback
        candidate_skills = set(s.strip().lower() for s in candidate.skills.split(','))
        required_skills = set(s.strip().lower() for s in job.required_skills.split(','))
        matched = candidate_skills.intersection(required_skills)
        match_percent = (len(matched) / len(required_skills)) * 100 if required_skills else 0
        is_match = match_percent >= 50 and candidate.experience >= job.min_experience

    AgentLog.objects.create(
        candidate=candidate,
        from_agent='Job Matching Agent',
        to_agent='Candidate Ranking Agent',
        message=f"AI Match Result: {'Matched' if is_match else 'Not Matched'}"
    )

    if is_match:
        ats_mcp_update(candidate, 'matched')
        return True

    ats_mcp_update(candidate, 'rejected')
    return False

# ============================================
# AGENT 3: Candidate Ranking Agent
# ============================================
def candidate_ranking_agent(candidates):
    from api.models import AgentLog
    print(f"[Agent 3] Ranking {len(candidates)} candidates")

    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)

    for i, candidate in enumerate(ranked):
        ats_mcp_update(candidate, 'ranked')
        AgentLog.objects.create(
            candidate=candidate,
            from_agent='Candidate Ranking Agent',
            to_agent='Interview Coordination Agent',
            message=f"Rank #{i+1}, Score: {candidate.score}"
        )
        print(f"[Agent 3] Rank #{i+1}: {candidate.name}")

    return ranked

# ============================================
# AGENT 4: Interview Coordination Agent
# ============================================
def interview_coordination_agent(candidate):
    from api.models import AgentLog
    print(f"[Agent 4] Scheduling interview: {candidate.name}")

    slot = calendar_mcp(candidate.name)

    candidate.interview_date = datetime.strptime(slot['slot'], '%Y-%m-%d %H:%M')
    ats_mcp_update(candidate, 'interview_scheduled')

    AgentLog.objects.create(
        candidate=candidate,
        from_agent='Interview Coordination Agent',
        to_agent='Recruiter Dashboard',
        message=f"Interview on {slot['slot']} via {slot['platform']}"
    )

    print(f"[Agent 4] Done. Slot: {slot['slot']}")
    return slot

# ============================================
# MAIN PIPELINE
# ============================================
def run_full_pipeline(candidate_id, job_id):
    from api.models import Candidate, Job

    candidate = Candidate.objects.get(id=candidate_id)
    job = Job.objects.get(id=job_id)

    print("===== PIPELINE START =====")

    candidate = resume_screening_agent(candidate)
    is_match = job_matching_agent(candidate, job)

    if is_match:
        ranked = candidate_ranking_agent([candidate])
        slot = interview_coordination_agent(ranked[0])
    else:
        slot = None

    print("===== PIPELINE END =====")
    return candidate, slot