import logging

from django.db import transaction
from django.shortcuts import render, get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from .agents import run_full_pipeline
from .models import Candidate, Job, AgentLog
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
from .embeddings import EmbeddingError
from .search import search_candidates
from .serializers import (
    CandidateCreateSerializer,
    JobCreateSerializer,
    PipelineRequestSerializer,
    SearchSerializer,
)
from .services import embed_candidate_chunks, rebuild_resume_chunks
from .throttles import PipelineThrottle, SearchThrottle
logger = logging.getLogger(__name__)

MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB


# Homepage (public: it only serves the HTML shell; all data comes from protected APIs)
def index(request):
    return render(request, 'index.html')


# ---------- Auth ----------
class LoginView(TokenObtainPairView):
    """POST username + password -> {access, refresh}. Strictly rate-limited."""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'


@api_view(['GET'])
def me(request):
    return Response({'username': request.user.get_username()})


# ---------- Create ----------
@api_view(['POST'])
def add_candidate(request):
    serializer = CandidateCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        c = serializer.save()
        chunk_count = rebuild_resume_chunks(c)
    embedded = embed_candidate_chunks(c)
    return Response({'id': c.id, 'message': 'Candidate added!', 'chunks': chunk_count,
                     'embedded_chunks': embedded}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
def add_job(request):
    serializer = JobCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    j = serializer.save()
    return Response({'id': j.id, 'message': 'Job added!'}, status=status.HTTP_201_CREATED)


# ---------- Pipeline ----------
@api_view(['POST'])
@throttle_classes([PipelineThrottle])
def run_pipeline(request):
    serializer = PipelineRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    candidate = get_object_or_404(Candidate, pk=serializer.validated_data['candidate_id'])
    job = get_object_or_404(Job, pk=serializer.validated_data['job_id'])

    candidate, slot = run_full_pipeline(candidate.id, job.id)
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
    })


# ---------- Read ----------
@api_view(['GET'])
def get_candidates(request):
    return Response(list(Candidate.objects.order_by('id').values()))


@api_view(['GET'])
def get_jobs(request):
    return Response(list(Job.objects.order_by('id').values()))


@api_view(['GET'])
def get_logs(request):
    logs = AgentLog.objects.order_by('id').values(
        'from_agent', 'to_agent', 'message', 'timestamp', 'candidate__name'
    )
    return Response(list(logs))


# ---------- Resume parsing ----------
@api_view(['POST'])
def parse_resume(request):
    """Upload a PDF / DOCX / TXT resume -> clean text + detected sections, contact and skills."""
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
@throttle_classes([SearchThrottle])
def search(request):
    """GET /api/search/?q=django rest apis&limit=5 -> best matching candidates + matching resume lines."""
    serializer = SearchSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    try:
        results = search_candidates(serializer.validated_data['q'], serializer.validated_data['limit'])
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