from django.contrib import admin
from .models import (AgentLog, AgentStep, Application, Candidate, Interview, Job, PipelineExecution,
                     RecruiterProfile, ResumeChunk)


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'owner', 'experience', 'score', 'status', 'created_at')
    list_filter = ('status', 'owner')
    search_fields = ('name', 'email', 'skills')


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('title', 'owner', 'min_experience', 'created_at')
    search_fields = ('title', 'required_skills')


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ('candidate', 'job', 'score', 'status', 'fallback_used', 'updated_at')
    list_filter = ('status', 'fallback_used', 'job')


@admin.register(AgentLog)
class AgentLogAdmin(admin.ModelAdmin):
    list_display = ('from_agent', 'to_agent', 'candidate', 'timestamp')
    list_filter = ('from_agent',)


@admin.register(ResumeChunk)
class ResumeChunkAdmin(admin.ModelAdmin):
    list_display = ('candidate', 'section', 'chunk_index', 'embedded', 'created_at')
    exclude = ('embedding',)    # 768 numbers are not useful to read
    list_filter = ('section',)
    search_fields = ('text', 'candidate__name')

    @admin.display(boolean=True, description='Embedded')
    def embedded(self, obj):
        return obj.embedding is not None


@admin.register(RecruiterProfile)
class RecruiterProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'created_at')
    search_fields = ('user__username', 'phone')


@admin.register(Interview)
class InterviewAdmin(admin.ModelAdmin):
    list_display = ('candidate', 'job', 'owner', 'scheduled_at', 'status')
    list_filter = ('status',)


class AgentStepInline(admin.TabularInline):
    model = AgentStep
    extra = 0
    readonly_fields = ('key', 'label', 'status', 'started_at', 'finished_at', 'duration_ms', 'message', 'error')
    can_delete = False


@admin.register(PipelineExecution)
class PipelineExecutionAdmin(admin.ModelAdmin):
    list_display = ('id', 'candidate', 'job', 'owner', 'status', 'created_at', 'finished_at')
    list_filter = ('status',)
    inlines = [AgentStepInline]
