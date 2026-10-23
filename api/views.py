import io
import logging

from django.shortcuts import render, get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from .agents import run_full_pipeline
from .models import Candidate, Job, AgentLog
from .serializers import (
    CandidateCreateSerializer,
    JobCreateSerializer,
    PipelineRequestSerializer,
)
from .throttles import PipelineThrottle

logger = logging.getLogger(__name__)

MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB



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
    c = serializer.save()
    return Response({'id': c.id, 'message': 'Candidate added!'}, status=status.HTTP_201_CREATED)


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
    import PyPDF2

    pdf_file = request.FILES.get('resume')
    if not pdf_file:
        return Response({'detail': "No file uploaded (form field name must be 'resume')."},
                        status=status.HTTP_400_BAD_REQUEST)
    if not pdf_file.name.lower().endswith('.pdf'):
        return Response({'detail': 'Only PDF files are supported.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if pdf_file.size > MAX_RESUME_BYTES:
        return Response({'detail': 'File too large (max 5 MB).'},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_file.read()))
        text = '\n'.join((page.extract_text() or '') for page in reader.pages)
    except Exception:
        logger.exception('PDF parsing failed')
        return Response({'detail': 'Could not read this PDF. It may be corrupted or encrypted.'},
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    if not text.strip():
        return Response({'detail': 'No text found in this PDF (scanned image?). Paste the text manually.'},
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    return Response({'text': text})