"""Advanced analytics dashboard: one endpoint with everything the recruiter's dashboard shows.

GET /api/analytics/?job=<id>   (job is optional: only that job's applications are counted)

Definitions (all counted per application, because one candidate can apply to many jobs):
    applied      every application
    shortlisted  passed screening: status shortlisted, matched, ranked or interview_scheduled
    matched      matched, ranked or interview_scheduled
    interview    interview_scheduled
    rejected     status rejected
Conversion rates are percentages of the previous stage and of all applications.
"""
from collections import Counter
from datetime import timedelta

from django.db.models import Avg
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import AgentStep, Application, Candidate, Interview, Job, PipelineExecution

SHORTLISTED = ('shortlisted', 'matched', 'ranked', 'interview_scheduled')
MATCHED = ('matched', 'ranked', 'interview_scheduled')
INTERVIEW = ('interview_scheduled',)
BUCKETS = (('0-39', 0, 39), ('40-59', 40, 59), ('60-79', 60, 79), ('80-100', 80, 100))
DAYS = 14


def _pct(part, whole) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _funnel(apps) -> list:
    total = len(apps)
    counts = [
        ('applied', 'Applied', total),
        ('shortlisted', 'Shortlisted', sum(1 for a in apps if a['status'] in SHORTLISTED)),
        ('matched', 'Matched', sum(1 for a in apps if a['status'] in MATCHED)),
        ('interview', 'Interview', sum(1 for a in apps if a['status'] in INTERVIEW)),
    ]
    out, previous = [], None
    for key, label, count in counts:
        out.append({'key': key, 'label': label, 'count': count, 'percent_of_applied': _pct(count, total),
                    'conversion_from_previous': None if previous is None else _pct(count, previous)})
        previous = count
    return out


def _job_rows(jobs, apps) -> list:
    rows = []
    for job in jobs:
        mine = [a for a in apps if a['job_id'] == job.pk]
        scored = [a['score'] for a in mine if a['status'] != 'pending']
        rows.append({
            'job_id': job.pk, 'title': job.title, 'applications': len(mine),
            'shortlisted': sum(1 for a in mine if a['status'] in SHORTLISTED),
            'rejected': sum(1 for a in mine if a['status'] == 'rejected'),
            'interviews': sum(1 for a in mine if a['status'] in INTERVIEW),
            'average_score': round(sum(scored) / len(scored), 1) if scored else 0,
            'top_score': round(max(scored), 1) if scored else 0,
            'match_rate': _pct(sum(1 for a in mine if a['status'] in SHORTLISTED), len(mine)),
            'conversion': _pct(sum(1 for a in mine if a['status'] in INTERVIEW), len(mine)),
        })
    return sorted(rows, key=lambda r: (-r['applications'], r['title'].lower()))


def _score_distribution(apps) -> list:
    scored = [a['score'] for a in apps if a['status'] != 'pending']
    return [{'range': label, 'count': sum(1 for s in scored if low <= round(s) <= high)}
            for label, low, high in BUCKETS]


def _by_day(apps, now) -> list:
    today = timezone.localtime(now).date()
    days = [today - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    counter = Counter(timezone.localtime(a['updated_at']).date() for a in apps if a['status'] != 'pending')
    return [{'date': d.isoformat(), 'count': counter.get(d, 0)} for d in days]


def _missing_skills(apps, limit=8) -> list:
    counter = Counter()
    for a in apps:
        evaluation = a['evaluation'] or {}
        for skill in (evaluation.get('skill_match') or {}).get('missing', []):
            counter[str(skill).lower()] += 1
    return [{'skill': skill, 'count': n} for skill, n in counter.most_common(limit)]


def _pipeline_health(owner, now) -> dict:
    since = now - timedelta(days=30)
    runs = PipelineExecution.objects.filter(owner=owner, created_at__gte=since)
    finished = [r for r in runs.filter(started_at__isnull=False, finished_at__isnull=False)]
    durations = [(r.finished_at - r.started_at).total_seconds() for r in finished]
    per_agent = (AgentStep.objects.filter(execution__owner=owner, execution__created_at__gte=since,
                                          status=AgentStep.Status.COMPLETED, duration_ms__isnull=False)
                 .values('key').annotate(avg=Avg('duration_ms')))
    return {
        'runs_30d': runs.count(),
        'completed': runs.filter(status=PipelineExecution.Status.COMPLETED).count(),
        'failed': runs.filter(status=PipelineExecution.Status.FAILED).count(),
        'average_seconds': round(sum(durations) / len(durations), 1) if durations else None,
        'agent_average_ms': {row['key']: round(row['avg']) for row in per_agent},
    }


def build_analytics(owner, job_id=None) -> dict:
    now = timezone.now()
    jobs = list(Job.objects.filter(owner=owner).order_by('id'))
    app_qs = Application.objects.filter(candidate__owner=owner, job__owner=owner)
    if job_id is not None:
        app_qs = app_qs.filter(job_id=job_id)
        jobs = [j for j in jobs if j.pk == job_id]
    apps = list(app_qs.values('job_id', 'candidate_id', 'status', 'score', 'updated_at', 'fallback_used', 'evaluation'))

    candidates = Candidate.objects.filter(owner=owner)
    interviews = Interview.objects.filter(owner=owner)
    if job_id is not None:
        interviews = interviews.filter(job_id=job_id)
    upcoming = (interviews.filter(status__in=Interview.ACTIVE, scheduled_at__gte=now)
                .select_related('candidate', 'job').order_by('scheduled_at')[:5])
    processed = {a['candidate_id'] for a in apps}
    funnel = _funnel(apps)

    return {
        'totals': {
            'candidates': candidates.count() if job_id is None else len(processed),
            'jobs': len(jobs),
            'applications': len(apps),
            'shortlisted': funnel[1]['count'],
            'rejected': sum(1 for a in apps if a['status'] == 'rejected'),
            'interviews_scheduled': interviews.filter(status__in=Interview.ACTIVE).count(),
            'interviews_completed': interviews.filter(status=Interview.Status.COMPLETED).count(),
            'interviews_cancelled': interviews.filter(status=Interview.Status.CANCELLED).count(),
            'not_yet_screened': candidates.exclude(pk__in=processed).count() if job_id is None else 0,
            'fallback_scores': sum(1 for a in apps if a['fallback_used']),
        },
        'conversion': {
            'shortlist_rate': funnel[1]['percent_of_applied'],
            'match_rate': funnel[2]['percent_of_applied'],
            'hiring_conversion': funnel[3]['percent_of_applied'],     # applied -> interview
            'rejection_rate': _pct(sum(1 for a in apps if a['status'] == 'rejected'), len(apps)),
        },
        'funnel': funnel,
        'jobs': _job_rows(jobs, apps),
        'score_distribution': _score_distribution(apps),
        'evaluations_by_day': _by_day(apps, now),
        'top_missing_skills': _missing_skills(apps),
        'pipeline_health': _pipeline_health(owner, now),
        'upcoming_interviews': [{
            'id': i.pk, 'candidate_id': i.candidate_id, 'candidate_name': i.candidate.name,
            'job_title': i.job.title, 'scheduled_at': i.scheduled_at, 'status': i.status,
        } for i in upcoming],
    }


@api_view(['GET'])
def analytics(request):
    job = request.query_params.get('job', '')
    if job and not job.isdigit():
        return Response({'detail': 'job must be a number.'}, status=http.HTTP_400_BAD_REQUEST)
    return Response(build_analytics(request.user, int(job) if job else None))
