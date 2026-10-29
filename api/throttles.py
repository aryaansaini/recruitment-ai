from rest_framework.throttling import UserRateThrottle


class PipelineThrottle(UserRateThrottle):
    """Each pipeline run costs Gemini calls, so limit it per user."""
    scope = 'pipeline'


class SearchThrottle(UserRateThrottle):
    """Each search costs one embedding call."""
    scope = 'search'


class PipelineDailyThrottle(UserRateThrottle):
    """...and per day, so one account cannot use up the shared free Gemini quota."""
    scope = 'pipeline_day'


class SearchDailyThrottle(UserRateThrottle):
    scope = 'search_day'