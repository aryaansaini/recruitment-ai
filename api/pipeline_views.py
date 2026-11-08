"""Live pipeline runs: start the four agents in the background and follow them.

POST /api/pipeline-runs/            {candidate_id, job_id}  -> 202 with the run (poll it)
GET  /api/pipeline-runs/<id>/       the run: status, one entry per agent (status, time taken, error), result
GET  /api/pipeline-runs/?limit=10   the latest runs
Everything is scoped to the logged-in recruiter: someone else's run answers 404.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status as http
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response

from . import pipeline_runs, tracking
from .models import Candidate, Job, PipelineExecution
from .serializers import PipelineRequestSerializer
from .throttles import PipelineDailyThrottle, PipelineThrottle


def _own(request, pk):
    execution = (PipelineExecution.objects.select_related('candidate', 'job')
                 .filter(pk=pk, owner=request.user).first())
    return tracking.expire_if_stale(execution) if execution else None


@api_view(['POST'])
@throttle_classes([PipelineThrottle, PipelineDailyThrottle])
def start_run(request):
    serializer = PipelineRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    candidate = get_object_or_404(Candidate, pk=serializer.validated_data['candidate_id'], owner=request.user)
    job = get_object_or_404(Job, pk=serializer.validated_data['job_id'], owner=request.user)
    execution, started = pipeline_runs.create_and_start(request.user, candidate, job)
    body = tracking.serialize(execution)
    body['already_running'] = not started
    return Response(body, status=http.HTTP_202_ACCEPTED)


@api_view(['GET'])
def get_run(request, pk):
    execution = _own(request, pk)
    if execution is None:
        return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)
    return Response(tracking.serialize(execution))


@api_view(['GET'])
def list_runs(request):
    try:
        limit = max(1, min(int(request.query_params.get('limit', 10)), 50))
    except ValueError:
        return Response({'detail': 'limit must be a number.'}, status=http.HTTP_400_BAD_REQUEST)
    rows = (PipelineExecution.objects.filter(owner=request.user)
            .select_related('candidate', 'job').prefetch_related('steps')[:limit])
    out = []
    for execution in rows:
        execution = tracking.expire_if_stale(execution)
        body = tracking.serialize(execution)
        body['result'] = ({k: body['result'].get(k) for k in ('score', 'status', 'rank')}
                          if body['result'] else None)       # the list does not need the whole evaluation
        out.append(body)
    return Response(out)
