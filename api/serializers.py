from rest_framework import serializers
from .models import Candidate, Job


class CandidateCreateSerializer(serializers.ModelSerializer):
    experience = serializers.IntegerField(min_value=0, max_value=60)
    resume_text = serializers.CharField(max_length=50000)

    class Meta:
        model = Candidate
        fields = ['name', 'email', 'phone', 'skills', 'experience', 'resume_text']
        extra_kwargs = {'skills': {'max_length': 1000}}


class JobCreateSerializer(serializers.ModelSerializer):
    min_experience = serializers.IntegerField(min_value=0, max_value=60)

    class Meta:
        model = Job
        fields = ['title', 'required_skills', 'min_experience', 'description']
        extra_kwargs = {
            'required_skills': {'max_length': 1000},
            'description': {'max_length': 5000},
        }


class PipelineRequestSerializer(serializers.Serializer):
    candidate_id = serializers.IntegerField(min_value=1)
    job_id = serializers.IntegerField(min_value=1)




class SearchSerializer(serializers.Serializer):
    q = serializers.CharField(min_length=2, max_length=300)
    limit = serializers.IntegerField(min_value=1, max_value=20, default=5)