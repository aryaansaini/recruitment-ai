from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .cv_matching import rank_jobs
from .models import Job


class CvMatchSerializer(serializers.Serializer):
    skills = serializers.ListField(child=serializers.CharField(max_length=60), max_length=200,
                                   required=False, default=list)
    experience = serializers.IntegerField(min_value=0, max_value=60, required=False, default=0)
    resume_text = serializers.CharField(max_length=300000, allow_blank=True, required=False, default='')


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def match_cv_view(request):
    """POST /api/match-cv/ - which of MY jobs is this CV eligible for? Nothing is saved."""
    serializer = CvMatchSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    jobs = list(Job.objects.filter(owner=request.user).values('id', 'title', 'required_skills', 'min_experience'))
    results = rank_jobs(jobs, data['skills'], data['experience'], data['resume_text'])
    return Response({
        'eligible_count': sum(1 for r in results if r['verdict'] == 'eligible'),
        'total_jobs': len(results),
        'jobs': results,
    })