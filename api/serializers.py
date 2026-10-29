import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
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


RESERVED_USERNAMES = {'admin', 'administrator', 'root', 'support', 'staff', 'system', 'recruiter', 'demo_recruiter'}


def normalize_phone(raw):
    """'+91 98765-43210' -> '+919876543210'. Raises ValidationError if it does not look like a phone number."""
    cleaned = re.sub(r'[\s().-]', '', str(raw or ''))
    if not re.fullmatch(r'\+?\d{10,15}', cleaned):
        raise serializers.ValidationError('Enter a valid phone number (10-15 digits, optional + country code).')
    return cleaned


class SignupSerializer(serializers.Serializer):
    username = serializers.RegexField(r'^[\w.@+-]+$', min_length=3, max_length=30,
                                      error_messages={'invalid': 'Use only letters, numbers and . @ + - _'})
    phone = serializers.CharField(max_length=30)
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)

    def validate_username(self, value):
        User = get_user_model()
        if value.lower() in RESERVED_USERNAMES or User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError('This username is not available.')
        return value

    def validate_phone(self, value):
        from .models import RecruiterProfile
        phone = normalize_phone(value)
        if RecruiterProfile.objects.filter(phone=phone).exists():
            raise serializers.ValidationError('This phone number is already registered.')
        return phone

    def validate(self, attrs):
        try:
            validate_password(attrs['password'], get_user_model()(username=attrs['username']))
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs