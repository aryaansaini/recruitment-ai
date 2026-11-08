"""Interview management endpoints (all scoped to the logged-in recruiter, someone else's record is a 404).

GET   /api/interviews/?status=&from=&to=     list (calendar and list view)
GET   /api/interviews/slots/?days=7&start=YYYY-MM-DD&application=<id>   free slots
POST  /api/applications/<id>/interview/      {scheduled_at, duration_minutes?, notes?}  schedule
PATCH /api/interviews/<id>/                  {scheduled_at, reason?, duration_minutes?}  reschedule
POST  /api/interviews/<id>/cancel/           {reason?}
POST  /api/interviews/<id>/complete/         {notes?}
"""
from datetime import date

from rest_framework import serializers
from rest_framework import status as http
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import interviews
from .interviews import InterviewError
from .models import Application, Interview
from .views import _demo_blocked


class ScheduleSerializer(serializers.Serializer):
    scheduled_at = serializers.DateTimeField()
    duration_minutes = serializers.IntegerField(min_value=15, max_value=interviews.MAX_DURATION, required=False)
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True, default='')
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True, default='')


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True, default='')
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True, default='')


def _own_interview(request, pk):
    return (Interview.objects.filter(pk=pk, owner=request.user)
            .select_related('candidate', 'job', 'application').first())


def _error(exc):
    return Response({'detail': str(exc)}, status=exc.status)


def _parse_date(raw):
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


@api_view(['GET'])
def list_interviews(request):
    qs = Interview.objects.filter(owner=request.user).select_related('candidate', 'job')
    wanted = request.query_params.get('status', '')
    if wanted:
        if wanted not in Interview.Status.values:
            return Response({'detail': 'status must be one of: ' + ', '.join(Interview.Status.values)},
                            status=http.HTTP_400_BAD_REQUEST)
        qs = qs.filter(status=wanted)
    for param, lookup in (('from', 'scheduled_at__date__gte'), ('to', 'scheduled_at__date__lte')):
        raw = request.query_params.get(param, '')
        if raw:
            day = _parse_date(raw)
            if day is None:
                return Response({'detail': f'{param} must be a date like 2026-11-09.'}, status=http.HTTP_400_BAD_REQUEST)
            qs = qs.filter(**{lookup: day})
    return Response([interviews.serialize(i) for i in qs.order_by('scheduled_at')[:500]])


@api_view(['GET'])
def free_slots(request):
    try:
        days = int(request.query_params.get('days', 7))
    except ValueError:
        return Response({'detail': 'days must be a number.'}, status=http.HTTP_400_BAD_REQUEST)
    start = None
    if request.query_params.get('start'):
        start = _parse_date(request.query_params['start'])
        if start is None:
            return Response({'detail': 'start must be a date like 2026-11-09.'}, status=http.HTTP_400_BAD_REQUEST)
    application = request.query_params.get('application', '')
    exclude = int(application) if application.isdigit() else None   # the interview being moved does not block itself
    return Response({'slots': interviews.available_slots(request.user, days=days, start_date=start,
                                                         exclude_application_id=exclude)})


@api_view(['POST'])
def schedule_interview(request, pk):
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    app = (Application.objects.filter(pk=pk, candidate__owner=request.user, job__owner=request.user)
           .select_related('candidate', 'job').first())
    if app is None:
        return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)
    serializer = ScheduleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        interview = interviews.schedule(app, data['scheduled_at'],
                                        duration=data.get('duration_minutes', interviews.DEFAULT_DURATION),
                                        notes=data['notes'])
    except InterviewError as exc:
        return _error(exc)
    return Response(interviews.serialize(interview), status=http.HTTP_201_CREATED)


@api_view(['PATCH'])
def reschedule_interview(request, pk):
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    interview = _own_interview(request, pk)
    if interview is None:
        return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)
    serializer = ScheduleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        interviews.reschedule(interview, data['scheduled_at'], reason=data['reason'],
                              duration=data.get('duration_minutes'))
    except InterviewError as exc:
        return _error(exc)
    return Response(interviews.serialize(interview))


@api_view(['POST'])
def cancel_interview(request, pk):
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    interview = _own_interview(request, pk)
    if interview is None:
        return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)
    serializer = ReasonSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        interviews.cancel(interview, reason=serializer.validated_data['reason'])
    except InterviewError as exc:
        return _error(exc)
    return Response(interviews.serialize(interview))


@api_view(['POST'])
def complete_interview(request, pk):
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    interview = _own_interview(request, pk)
    if interview is None:
        return Response({'detail': 'Not found.'}, status=http.HTTP_404_NOT_FOUND)
    serializer = ReasonSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        interviews.complete(interview, notes=serializer.validated_data['notes'])
    except InterviewError as exc:
        return _error(exc)
    return Response(interviews.serialize(interview))
