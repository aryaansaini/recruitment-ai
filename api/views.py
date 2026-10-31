import logging

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404, render
from rest_framework import status
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from .agents import run_full_pipeline
from .auth_views import DEMO_USERNAME
from .embeddings import EmbeddingError
from .models import AgentLog, Candidate, Job

from .leaderboard import job_leaderboard

from .resume_parser import (
    ALLOWED_EXTENSIONS,
    ResumeParseError,
    UnsupportedResumeType,
    chunk_resume,
    extract_email,
    extract_phone,
    extract_text,
    parse_skill_list,
    split_sections,
)
from .search import search_candidates
from .serializers import (
    CandidateCreateSerializer,
    JobCreateSerializer,
    PipelineRequestSerializer,
    SearchSerializer,
)
from .services import embed_candidate_chunks, rebuild_resume_chunks
from .throttles import (
    PipelineDailyThrottle,
    PipelineThrottle,
    SearchDailyThrottle,
    SearchThrottle,
)

logger = logging.getLogger(__name__)

MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB


# Homepage (public: it only serves the HTML shell; all data comes from protected APIs)
def index(request):
    return render(request, 'index.html')


def _demo_blocked(request):
    """The shared demo workspace is read-only (everyone sees the same data): block all writes there."""
    if request.user.username == DEMO_USERNAME:
        return Response({'detail': 'The demo workspace is read-only. Create a free account to add your own '
                                   'jobs and resumes.'}, status=status.HTTP_403_FORBIDDEN)
    return None


def _limit_reached(user, model, limit):
    """Per-account quota. Staff (the site owner) is exempt."""
    return not user.is_staff and model.objects.filter(owner=user).count() >= limit


# ---------- Auth ----------
class LoginView(TokenObtainPairView):
    """POST username + password -> {access, refresh}. Strictly rate-limited."""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'


@api_view(['GET', 'DELETE'])
def me(request):
    user = request.user
    if request.method == 'DELETE':
        if user.is_staff or user.username == DEMO_USERNAME:
            return Response({'detail': 'This account cannot be deleted here.'}, status=status.HTTP_403_FORBIDDEN)
        if not user.check_password(str(request.data.get('password', ''))):
            return Response({'detail': 'Wrong password.'}, status=status.HTTP_400_BAD_REQUEST)
        user.delete()        # cascades: jobs, candidates, resume chunks (vectors), applications, agent logs
        return Response(status=status.HTTP_204_NO_CONTENT)
    return Response({
        'username': user.get_username(),
        'is_staff': user.is_staff,
        'is_demo': user.username == DEMO_USERNAME,
        'phone': getattr(getattr(user, 'profile', None), 'phone', None),
        'usage': {'candidates': Candidate.objects.filter(owner=user).count(),
                  'jobs': Job.objects.filter(owner=user).count()},
        'limits': None if user.is_staff else {'candidates': settings.MAX_CANDIDATES_PER_USER,
                                              'jobs': settings.MAX_JOBS_PER_USER},
    })


# ---------- Create ----------
@api_view(['POST'])
def add_candidate(request):
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    if _limit_reached(request.user, Candidate, settings.MAX_CANDIDATES_PER_USER):
        return Response({'detail': f'Limit reached: {settings.MAX_CANDIDATES_PER_USER} candidates per account '
                                   'on this demo site. Delete one to add another.'},
                        status=status.HTTP_403_FORBIDDEN)
    serializer = CandidateCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        c = serializer.save(owner=request.user)               # owner always comes from the login, never the payload
        chunk_count = rebuild_resume_chunks(c)
    # Outside the DB transaction: a slow/failed embedding call must never lose the candidate
    embedded = embed_candidate_chunks(c)
    return Response({'id': c.id, 'message': 'Candidate added!', 'chunks': chunk_count,
                     'embedded_chunks': embedded}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
def add_job(request):
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    if _limit_reached(request.user, Job, settings.MAX_JOBS_PER_USER):
        return Response({'detail': f'Limit reached: {settings.MAX_JOBS_PER_USER} jobs per account '
                                   'on this demo site. Delete one to add another.'},
                        status=status.HTTP_403_FORBIDDEN)
    serializer = JobCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    j = serializer.save(owner=request.user)
    return Response({'id': j.id, 'message': 'Job added!'}, status=status.HTTP_201_CREATED)


# ---------- Delete ----------
@api_view(['DELETE'])
def delete_candidate(request, pk):
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    get_object_or_404(Candidate, pk=pk, owner=request.user).delete()   # chunks, applications, logs cascade
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['DELETE'])
def delete_job(request, pk):
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    get_object_or_404(Job, pk=pk, owner=request.user).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ---------- Pipeline ----------
@api_view(['POST'])
@throttle_classes([PipelineThrottle, PipelineDailyThrottle])
def run_pipeline(request):
    serializer = PipelineRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    candidate = get_object_or_404(Candidate, pk=serializer.validated_data['candidate_id'], owner=request.user)
    job = get_object_or_404(Job, pk=serializer.validated_data['job_id'], owner=request.user)

    candidate, slot = run_full_pipeline(candidate.id, job.id)
    standing = next((r for r in job_leaderboard(job) if r['candidate_id'] == candidate.id), None)
    return Response({
        'name': candidate.name,
        'status': candidate.status,
        'score': candidate.score,
        'reason': getattr(candidate, 'ai_reason', ''),
        'fallback_used': getattr(candidate, 'fallback_used', False),
        'breakdown': getattr(candidate, 'ai_breakdown', None),
        'evidence': getattr(candidate, 'ai_evidence', []),
        'retrieval': getattr(candidate, 'ai_retrieval', ''),
        'interview_slot': slot,
        'rank': standing['rank'] if standing else None,
    })


# ---------- Read (always only the logged-in recruiter's own data) ----------
@api_view(['GET'])
def get_candidates(request):
    return Response(list(Candidate.objects.filter(owner=request.user).order_by('id').values()))


@api_view(['GET'])
def get_jobs(request):
    return Response(list(Job.objects.filter(owner=request.user).order_by('id').values()))


@api_view(['GET'])
def get_logs(request):
    latest = AgentLog.objects.filter(candidate__owner=request.user).order_by('-id').values(
        'from_agent', 'to_agent', 'message', 'timestamp', 'candidate__name'
    )[:200]
    return Response(list(latest)[::-1])      # oldest first, like before


# ---------- Resume parsing ----------
@api_view(['POST'])
def parse_resume(request):
    """Upload a PDF / DOCX / TXT resume -> clean text + detected sections, contact and skills."""
    blocked = _demo_blocked(request)
    if blocked:
        return blocked
    resume_file = request.FILES.get('resume')
    if not resume_file:
        return Response({'detail': "No file uploaded (form field name must be 'resume')."},
                        status=status.HTTP_400_BAD_REQUEST)
    if not resume_file.name.lower().endswith(ALLOWED_EXTENSIONS):
        return Response({'detail': 'Only PDF, DOCX or TXT files are supported.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if resume_file.size > MAX_RESUME_BYTES:
        return Response({'detail': 'File too large (max 5 MB).'},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        text = extract_text(resume_file.read(), resume_file.name)
    except UnsupportedResumeType as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except ResumeParseError as exc:
        logger.warning('Resume parsing failed for %s: %s', resume_file.name, exc)
        return Response({'detail': str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    sections = split_sections(text)
    return Response({
        'text': text,
        'sections': [name for name in sections if name != 'header'],
        'email': extract_email(text),
        'phone': extract_phone(text),
        'skills': parse_skill_list(sections.get('skills', '')),
        'chunk_count': len(chunk_resume(text)),
    })


# ---------- Semantic search ----------
@api_view(['GET'])
@throttle_classes([SearchThrottle, SearchDailyThrottle])
def search(request):
    """GET /api/search/?q=django rest apis&limit=5 -> best matching candidates + matching resume lines."""
    serializer = SearchSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    try:
        results = search_candidates(serializer.validated_data['q'], serializer.validated_data['limit'],
                                    owner=request.user)
    except EmbeddingError as exc:
        logger.warning('Search failed: %s', exc)
        return Response({'detail': 'Search is temporarily unavailable (embedding service error).'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response([{
        'candidate_id': r['candidate'].id,
        'name': r['candidate'].name,
        'email': r['candidate'].email,
        'skills': r['candidate'].skills,
        'experience': r['candidate'].experience,
        'similarity': round(r['similarity'], 3),
        'evidence': r['evidence'],
    } for r in results])