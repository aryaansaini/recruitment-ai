from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Application, Job
from .ranking import build_leaderboard


def job_leaderboard(job):
    """Ranked list of everyone who applied to `job` (rank is computed, never stored)."""
    applications = Application.objects.filter(job=job).select_related('candidate')
    rows = [{
        'application_id': a.pk,
        'candidate_id': a.candidate_id,
        'name': a.candidate.name,
        'score': a.score,
        'status': a.status,
        'fallback_used': a.fallback_used,
        'interview_date': a.interview_date,
    } for a in applications]
    return build_leaderboard(rows)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def job_leaderboard_view(request, pk):
    """GET /api/jobs/<id>/leaderboard/ - only for the recruiter's own jobs (404 otherwise)."""
    job = Job.objects.filter(pk=pk, owner=request.user).first()
    if job is None:
        return Response({'detail': 'Not found.'}, status=404)
    return Response({'job': {'id': job.pk, 'title': job.title}, 'candidates': job_leaderboard(job)})