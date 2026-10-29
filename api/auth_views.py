import hmac
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from .models import RecruiterProfile
from .serializers import SignupSerializer

DEMO_USERNAME = 'demo_recruiter'


class DemoThrottle(AnonRateThrottle):
    scope = 'demo'
    rate = '30/hour'


class SignupThrottle(AnonRateThrottle):
    scope = 'signup'
    rate = '10/hour'          # per IP address


def _tokens(user):
    refresh = RefreshToken.for_user(user)
    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'username': user.username,
    }


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([DemoThrottle])
def demo_login(request):
    """One-click login to the shared, READ-ONLY demo workspace (see seed_demo)."""
    User = get_user_model()
    user, created = User.objects.get_or_create(username=DEMO_USERNAME)
    if created:
        user.set_unusable_password()   # normal password login impossible
        user.save()
    return Response(_tokens(user))


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def auth_config(request):
    """Public: lets the login screen know whether sign-up is open and if an invite code is needed."""
    return Response({
        'signups_enabled': settings.SIGNUPS_ENABLED,
        'invite_required': bool(settings.SIGNUP_INVITE_CODE),
    })


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([SignupThrottle])
def signup(request):
    """Create a recruiter account (username + phone + password) and log it in."""
    if not settings.SIGNUPS_ENABLED:
        return Response({'detail': 'Sign-ups are closed right now.'}, status=403)

    required = settings.SIGNUP_INVITE_CODE
    if required and not hmac.compare_digest(str(request.data.get('invite_code', '')), required):
        return Response({'detail': 'A valid invite code is required.'}, status=403)

    User = get_user_model()
    since = timezone.now() - timedelta(days=1)
    if User.objects.filter(date_joined__gte=since).count() >= settings.MAX_SIGNUPS_PER_DAY:
        return Response({'detail': 'The daily sign-up limit was reached. Please try again tomorrow.'}, status=403)

    serializer = SignupSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    with transaction.atomic():
        user = User.objects.create_user(username=data['username'], password=data['password'])
        RecruiterProfile.objects.create(user=user, phone=data['phone'])
    return Response(_tokens(user), status=201)