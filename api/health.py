from django.http import JsonResponse
from django.views.decorators.http import require_safe


@require_safe
def healthz(request):
    """Liveness probe for Render / UptimeRobot. Public, no login, no database access."""
    return JsonResponse({'status': 'ok'})