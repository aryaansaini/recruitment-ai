from django.db import models


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
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Job(models.Model):
    title = models.CharField(max_length=100)
    required_skills = models.TextField()    # e.g. Python, Django, SQL
    min_experience = models.IntegerField()
    description = models.TextField()
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['candidate_id', 'chunk_index']
        constraints = [
            models.UniqueConstraint(fields=['candidate', 'chunk_index'], name='unique_chunk_index_per_candidate'),
        ]

    def __str__(self):
        return f'{self.candidate} [{self.section} #{self.chunk_index}]'