"""Endpoints behind the candidate detail page and the Kanban board.

Everything is scoped to the logged-in recruiter: someone else's records answer 404.
"""
from rest_framework import status as http
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import AgentLog, Application, Candidate, Job
from .views import _demo_blocked

CANDIDATE_FIELDS = ('id', 'name', 'email', 'phone', 'skills', 'experience', 'resume_text',
                    'score', 'status', 'interview_date', 'created_at')
APPLICATION_FIELDS = ('id', 'score', 'status', 'reason', 'fallback_used', 'breakdown',
                      'evidence', 'interview_date', 'updated_at')


@api_view(['GET'])
def candidate_detail(request, pk):
    """GET /api/candidates/<id>/detail/ - profile, every application with its evidence, agent timeline."""
    c = Candidate.objects.filter(pk=pk, owner=request.user).first()
    if c is None:
        return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)
    applications = [
        {**{f: getattr(a, f) for f in APPLICATION_FIELDS}, 'job_id': a.job_id, 'job_title': a.job.title}
        for a in c.applications.select_related('job').order_by('-updated_at')
    ]
    logs = list(AgentLog.objects.filter(candidate=c).order_by('id')
                .values('from_agent', 'to_agent', 'message', 'timestamp'))
    return Response({
        'candidate': {f: getattr(c, f) for f in CANDIDATE_FIELDS},
        'applications': applications,
        'logs': logs,
    })


@api_view(['GET'])
def list_applications(request):
    """GET /api/applications/?job=<id> - the cards of the Kanban board (all of my jobs, or one)."""
    qs = Application.objects.filter(candidate__owner=request.user, job__owner=request.user)
    job = request.query_params.get('job', '')
    if job:
        if not job.isdigit():
            return Response({'detail': 'job must be a number.'}, status=http.HTTP_400_BAD_REQUEST)
        qs = qs.filter(job_id=int(job))
    rows = qs.select_related('candidate', 'job').order_by('-score', '-updated_at')
    return Response([{
        'id': a.pk, 'candidate_id': a.candidate_id, 'candidate_name': a.candidate.name,
        'job_id': a.job_id, 'job_title': a.job.title, 'score': a.score, 'status': a.status,
        'fallback_used': a.fallback_used, 'interview_date': a.interview_date,
    } for a in rows])


@api_view(['PATCH'])
def set_application_status(request, pk):
    """PATCH /api/applications/<id>/status/ {"status": "rejected"} - move a card to another column."""
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    app = (Application.objects.filter(pk=pk, candidate__owner=request.user, job__owner=request.user)
           .select_related('candidate', 'job').first())
    if app is None:
        return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)
    new = str(request.data.get('status', ''))
    if new not in Application.Status.values:
        return Response({'detail': 'status must be one of: ' + ', '.join(Application.Status.values)},
                        status=http.HTTP_400_BAD_REQUEST)
    app.status = new
    app.save(update_fields=['status', 'updated_at'])
    AgentLog.objects.create(candidate=app.candidate, from_agent='Recruiter', to_agent='Application board',
                            message=f'Moved the application for {app.job.title} to {new}.')
    return Response({'id': app.pk, 'status': app.status})
