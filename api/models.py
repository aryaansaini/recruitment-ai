from django.conf import settings
from django.db import models
from pgvector.django import VectorField
from django.utils import timezone

class Candidate(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SHORTLISTED = 'shortlisted', 'Shortlisted'
        MATCHED = 'matched', 'Matched'
        RANKED = 'ranked', 'Ranked'
        INTERVIEW_SCHEDULED = 'interview_scheduled', 'Interview scheduled'
        REJECTED = 'rejected', 'Rejected'

    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=15)
    skills = models.TextField()         
    experience = models.IntegerField()   
    resume_text = models.TextField()
    score = models.FloatField(default=0)
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.PENDING)
    interview_date = models.DateTimeField(null=True, blank=True)
    # Every candidate belongs to ONE recruiter; nobody else can see it (deleting the user deletes it)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.CASCADE, related_name='candidates')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Job(models.Model):
    title = models.CharField(max_length=100)
    required_skills = models.TextField()    # e.g. Python, Django, SQL
    min_experience = models.IntegerField()
    description = models.TextField()
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.CASCADE, related_name='jobs')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class Application(models.Model):
    """One candidate applying to one job. Holds the per-job score and status."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SHORTLISTED = 'shortlisted', 'Shortlisted'
        MATCHED = 'matched', 'Matched'
        RANKED = 'ranked', 'Ranked'
        INTERVIEW_SCHEDULED = 'interview_scheduled', 'Interview scheduled'
        REJECTED = 'rejected', 'Rejected'

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='applications')
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='applications')
    score = models.FloatField(default=0)
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.PENDING)
    reason = models.TextField(blank=True)           # why the AI gave this score
    fallback_used = models.BooleanField(default=False)  # True if rule-based fallback scored it (AI failed)
    breakdown = models.JSONField(null=True, blank=True)  # {"skills": 80, "experience": 100, "education": 50}
    evidence = models.JSONField(default=list, blank=True)  # verified resume quotes behind the score
    interview_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['candidate', 'job'], name='unique_candidate_job_application'),
        ]
        ordering = ['-score', '-created_at']

    def __str__(self):
        return f'{self.candidate} -> {self.job} ({self.status})'


class AgentLog(models.Model):
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE)
    from_agent = models.CharField(max_length=100)
    to_agent = models.CharField(max_length=100)
    message = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.from_agent} -> {self.to_agent}'



# Resume split into section-aware chunks (Day 5). Day 6 adds an embedding vector to each.
class ResumeChunk(models.Model):
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='chunks')
    section = models.CharField(max_length=30)       # skills, experience, projects, ...
    chunk_index = models.PositiveIntegerField()     # order inside the resume
    text = models.TextField()
    embedding = VectorField(dimensions=768, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['candidate_id', 'chunk_index']
        constraints = [
            models.UniqueConstraint(fields=['candidate', 'chunk_index'], name='unique_chunk_index_per_candidate'),
        ]

    @property
    def embed_text(self):
        """Text sent to the embedding model: the section label gives it context."""
        return f'{self.section.title()}: {self.text}'
        
    def __str__(self):
        return f'{self.candidate} [{self.section} #{self.chunk_index}]'


class RecruiterProfile(models.Model):
    """Extra details collected at sign-up. The phone number is NOT verified (no SMS check)."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    phone = models.CharField(max_length=16, unique=True, null=True, blank=True)  # old field, no longer collected
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user} ({self.phone})'

    
class EmailOTP(models.Model):
    """Pending email verification for a new sign-up. The code is stored hashed, never in plain text."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_otp')
    code_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    last_sent_at = models.DateTimeField(default=timezone.now)