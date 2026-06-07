from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import Candidate, Job, AgentLog
from .agents import run_full_pipeline

# Homepage
def index(request):
    return render(request, 'index.html')

# Add Candidate
@api_view(['POST'])
def add_candidate(request):
    data = request.data
    c = Candidate.objects.create(
        name=data['name'],
        email=data['email'],
        phone=data['phone'],
        skills=data['skills'],
        experience=int(data['experience']),
        resume_text=data['resume_text']
    )
    return Response({'id': c.id, 'message': 'Candidate added!'})

# Add Job
@api_view(['POST'])
def add_job(request):
    data = request.data
    j = Job.objects.create(
        title=data['title'],
        required_skills=data['required_skills'],
        min_experience=int(data['min_experience']),
        description=data['description']
    )
    return Response({'id': j.id, 'message': 'Job added!'})

# Run Pipeline
@api_view(['POST'])
def run_pipeline(request):
    candidate_id = request.data.get('candidate_id')
    job_id = request.data.get('job_id')
    candidate, slot = run_full_pipeline(candidate_id, job_id)
    return Response({
        'name': candidate.name,
        'status': candidate.status,
        'score': candidate.score,
        'interview_slot': slot
    })

# Get all candidates
@api_view(['GET'])
def get_candidates(request):
    candidates = Candidate.objects.all().values()
    return Response(list(candidates))

# Get all jobs
@api_view(['GET'])
def get_jobs(request):
    jobs = Job.objects.all().values()
    return Response(list(jobs))

# Get Agent Logs (A2A Communication)
@api_view(['GET'])
def get_logs(request):
    logs = AgentLog.objects.all().values(
        'from_agent', 'to_agent', 'message', 
        'timestamp', 'candidate__name'
    )
    return Response(list(logs))

@api_view(['POST'])
def parse_resume(request):
    import PyPDF2
    import io
    pdf_file = request.FILES.get('resume')
    if not pdf_file:
        return Response({'text': ''})
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_file.read()))
        text = ''
        for page in reader.pages:
            text += page.extract_text()
        return Response({'text': text})
    except:
        return Response({'text': ''})