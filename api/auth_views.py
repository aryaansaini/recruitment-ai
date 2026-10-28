from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

DEMO_USERNAME = 'demo_recruiter'


class DemoThrottle(AnonRateThrottle):
    scope = 'demo'
    rate = '30/hour'


class SignupThrottle(AnonRateThrottle):
    scope = 'signup'
    rate = '10/hour'


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
    user, created = User.objects.get_or_create(username=DEMO_USERNAME)
    if created:
        user.set_unusable_password()   # normal password login impossible
        user.save()
    return Response(_tokens(user))


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([SignupThrottle])
def signup(request):
    username = (request.data.get('username') or '').strip()
    password = request.data.get('password') or ''
    errors = {}

    if not 3 <= len(username) <= 30:
        errors['username'] = ['Username must be 3-30 characters.']
    elif username.lower() == DEMO_USERNAME or User.objects.filter(username__iexact=username).exists():
        errors['username'] = ['This username is taken.']

    try:
        validate_password(password)
    except ValidationError as e:
        errors['password'] = list(e.messages)

    if errors:
        return Response(errors, status=400)

    user = User.objects.create_user(username=username, password=password)
    return Response(_tokens(user), status=201)