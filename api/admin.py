from django.contrib import admin
from .models import Candidate, Job, Application, AgentLog


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'experience', 'score', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'email', 'skills')


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('title', 'min_experience', 'created_at')
    search_fields = ('title', 'required_skills')


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ('candidate', 'job', 'score', 'status', 'fallback_used', 'updated_at')
    list_filter = ('status', 'fallback_used', 'job')


@admin.register(AgentLog)
class AgentLogAdmin(admin.ModelAdmin):
    list_display = ('from_agent', 'to_agent', 'candidate', 'timestamp')
    list_filter = ('from_agent',)