from rest_framework.throttling import UserRateThrottle


class PipelineThrottle(UserRateThrottle):
    """Each pipeline run costs Gemini calls, so limit it per user."""
    scope = 'pipeline'