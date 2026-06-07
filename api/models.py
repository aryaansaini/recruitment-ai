from django.db import models

# MCP Server 1: ATS MCP - Candidate data store karta hai
class Candidate(models.Model):
    # Candidate basic info
    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=15)
    skills = models.TextField()          # e.g. Python, Django, SQL
    experience = models.IntegerField()   # years mein
    resume_text = models.TextField()     # MCP: Resume Parser MCP
    score = models.FloatField(default=0)
    
    # Agent status track karta hai
    status = models.CharField(max_length=50, default='pending')
    # pending → shortlisted → matched → ranked → interview_scheduled → rejected
    
    interview_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


# MCP Server 1: ATS MCP - Job data store karta hai
class Job(models.Model):
    title = models.CharField(max_length=100)
    required_skills = models.TextField()    # e.g. Python, Django, SQL
    min_experience = models.IntegerField()
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


# A2A Communication log - kaun sa agent kisne bheja
class AgentLog(models.Model):
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE)
    from_agent = models.CharField(max_length=100)
    to_agent = models.CharField(max_length=100)
    message = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.from_agent} → {self.to_agent}"