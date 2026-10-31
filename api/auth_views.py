import hashlib
import hmac
import secrets
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    AgentLog, Application, Candidate, EmailOTP, Job, RecruiterProfile, ResumeChunk,
)

User = get_user_model()
logger = logging.getLogger(__name__)
DEMO_USERNAME = 'demo_recruiter'      # read-only TEMPLATE account (nobody can log in to it)
TEMP_PREFIX = 'demo_session_'         # every demo visitor gets a throw-away account with this prefix



# ---------- throttles ----------
class DemoThrottle(AnonRateThrottle):
    scope = 'demo'
    rate = '30/hour'


class SignupThrottle(AnonRateThrottle):
    scope = 'signup'
    rate = '10/hour'          # per IP address


class OtpThrottle(AnonRateThrottle):
    scope = 'otp'
    rate = '40/hour'          # verify + resend, per IP address


# ---------- helpers ----------
def _tokens(user):
    refresh = RefreshToken.for_user(user)
    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
        'username': user.username,
    }


def _otp_ttl():
    return timedelta(minutes=getattr(settings, 'OTP_TTL_MINUTES', 10))


def _otp_required():
    """Email OTP is optional for now. Turn it on with EMAIL_OTP_REQUIRED=True (env var)."""
    return bool(getattr(settings, 'EMAIL_OTP_REQUIRED', False))


def _otp_cooldown():
    return int(getattr(settings, 'OTP_RESEND_SECONDS', 60))


def _otp_max_attempts():
    return int(getattr(settings, 'OTP_MAX_ATTEMPTS', 5))


def _hash_code(user_id, code):
    msg = f'{user_id}:{code}'.encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def _mask(email):
    name, _, domain = email.partition('@')
    return f'{name[:1]}***@{domain}'


def _issue_otp(user, purpose='signup'):
    """Create a fresh 6-digit code (stored hashed) and email it. Raises if the email fails."""
    code = f'{secrets.randbelow(10 ** 6):06d}'
    now = timezone.now()
    EmailOTP.objects.update_or_create(user=user, defaults={
        'code_hash': _hash_code(user.pk, code),
        'expires_at': now + _otp_ttl(),
        'attempts': 0,
        'last_sent_at': now,
    })
    minutes = int(_otp_ttl().total_seconds() // 60)
    if purpose == 'reset':
        subject = 'Your Recruit AI password reset code'
        body = (f'Your Recruit AI password reset code is {code}.\n\n'
                f'It expires in {minutes} minutes. If you did not ask to reset your password, ignore this email.')
    else:
        subject = 'Your Recruit AI verification code'
        body = (f'Your Recruit AI verification code is {code}.\n\n'
                f'It expires in {minutes} minutes. If you did not sign up, ignore this email.')
    send_mail(subject, body, None, [user.email], fail_silently=False)


def _purge_stale_unverified():
    """Sign-ups that never verified their email are removed after a day."""
    old = timezone.now() - timedelta(days=1)
    User.objects.filter(is_active=False, is_staff=False, email_otp__isnull=False,
                        date_joined__lt=old).delete()


# ---------- sign-up with email OTP ----------
class EmailSignupSerializer(serializers.Serializer):
    username = serializers.RegexField(
        r'^[A-Za-z0-9_.-]{3,30}$',
        error_messages={'invalid': 'Username: 3-30 letters, numbers, dot, dash or underscore.'})
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, write_only=True, trim_whitespace=False)

    def validate_username(self, value):
        if value.lower().startswith('demo'):
            raise serializers.ValidationError('This username is reserved.')
        if User.objects.filter(username__iexact=value, is_active=True).exists():
            raise serializers.ValidationError('This username is already taken.')
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value, is_active=True).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return value

    def validate(self, attrs):
        try:
            validate_password(attrs['password'],
                              user=User(username=attrs['username'], email=attrs['email']))
        except DjangoValidationError as e:
            raise serializers.ValidationError({'password': list(e.messages)})
        return attrs


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([SignupThrottle])
def signup(request):
    """OTP on: create an INACTIVE account and email a 6-digit code (no tokens yet).
    OTP off (default): create the account and log the user in right away."""
    if not settings.SIGNUPS_ENABLED:
        return Response({'detail': 'Sign-ups are closed right now.'}, status=403)

    required = settings.SIGNUP_INVITE_CODE
    if required and not hmac.compare_digest(str(request.data.get('invite_code', '')), required):
        return Response({'detail': 'A valid invite code is required.'}, status=403)

    _purge_stale_unverified()
    since = timezone.now() - timedelta(days=1)
    recent = User.objects.filter(date_joined__gte=since).exclude(username__startswith=TEMP_PREFIX)
    if recent.count() >= settings.MAX_SIGNUPS_PER_DAY:
        return Response({'detail': 'The daily sign-up limit was reached. Please try again tomorrow.'},
                        status=403)

    serializer = EmailSignupSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    otp_required = _otp_required()
    try:
        with transaction.atomic():
            # an older, never-verified attempt with the same username/email is replaced
            User.objects.filter(is_active=False, is_staff=False, email_otp__isnull=False).filter(
                Q(username__iexact=data['username']) | Q(email__iexact=data['email'])).delete()
            user = User.objects.create_user(username=data['username'], email=data['email'],
                                            password=data['password'], is_active=not otp_required)
            RecruiterProfile.objects.create(user=user)
    except IntegrityError:
        return Response({'detail': 'This username is already taken.'}, status=400)

    if not otp_required:
        return Response({**_tokens(user), 'email': _mask(data['email'])}, status=201)

    try:
        _issue_otp(user)
    except Exception:
        logger.exception('Could not send the sign-up OTP email')
        user.delete()
        return Response({'detail': 'Could not send the email right now. Please try again in a minute.'},
                        status=503)
    return Response({'detail': 'Verification code sent.', 'email': _mask(data['email'])}, status=201)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([OtpThrottle])
def verify_otp(request):
    """Step 2: check the code. Right code -> account becomes active and the user is logged in."""
    username = str(request.data.get('username', '')).strip()
    code = str(request.data.get('code', '')).strip()
    if len(code) != 6 or not code.isdigit():
        return Response({'detail': 'Enter the 6-digit code.'}, status=400)

    with transaction.atomic():
        otp = (EmailOTP.objects.select_for_update()
               .filter(user__username__iexact=username, user__is_active=False).first())
        if otp is None:
            return Response({'detail': 'Invalid or expired code.'}, status=400)
        if otp.expires_at < timezone.now():
            return Response({'detail': 'This code has expired. Request a new one.'}, status=400)
        if otp.attempts >= _otp_max_attempts():
            return Response({'detail': 'Too many wrong attempts. Request a new code.'}, status=400)
        if not hmac.compare_digest(otp.code_hash, _hash_code(otp.user_id, code)):
            otp.attempts += 1
            otp.save(update_fields=['attempts'])
            left = _otp_max_attempts() - otp.attempts
            return Response({'detail': f'Wrong code. {left} attempt(s) left.'}, status=400)
        user = otp.user
        user.is_active = True
        user.save(update_fields=['is_active'])
        otp.delete()
    return Response(_tokens(user))


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([OtpThrottle])
def resend_otp(request):
    username = str(request.data.get('username', '')).strip()
    otp = (EmailOTP.objects.select_related('user')
           .filter(user__username__iexact=username, user__is_active=False).first())
    generic = Response({'detail': 'If a pending sign-up exists, a new code was sent.'})
    if otp is None:
        return generic
    wait = _otp_cooldown() - (timezone.now() - otp.last_sent_at).total_seconds()
    if wait > 0:
        return Response({'detail': f'Please wait {int(wait) + 1} seconds before asking for a new code.'},
                        status=429)
    try:
        _issue_otp(otp.user)
    except Exception:
        return Response({'detail': 'Could not send the email right now. Please try again.'}, status=503)
    return generic


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def auth_config(request):
    """Public: lets the login screen know whether sign-up is open and if an invite code is needed."""
    return Response({
        'signups_enabled': settings.SIGNUPS_ENABLED,
        'invite_required': bool(settings.SIGNUP_INVITE_CODE),
        'email_otp_required': _otp_required(),
    })


# ---------- temporary demo sessions ----------
def _clone_demo_data(user):
    """Copy the sample workspace (owned by the template account) into a visitor's private account."""
    src = User.objects.filter(username=DEMO_USERNAME).first()
    if src is None:
        return

    def clone(obj, **changes):
        old = obj.pk
        obj.pk = None
        obj._state.adding = True
        for k, v in changes.items():
            setattr(obj, k, v)
        obj.save()
        return old, obj

    with transaction.atomic():
        jobs = dict(clone(j, owner=user) for j in list(Job.objects.filter(owner=src)))
        cands = dict(clone(c, owner=user) for c in list(Candidate.objects.filter(owner=src)))
        for a in list(Application.objects.filter(candidate__owner=src)):
            if a.candidate_id in cands and a.job_id in jobs:
                clone(a, candidate=cands[a.candidate_id], job=jobs[a.job_id])
        chunks = []
        for ch in ResumeChunk.objects.filter(candidate__owner=src):
            if ch.candidate_id in cands:
                ch.pk = None
                ch._state.adding = True
                ch.candidate = cands[ch.candidate_id]
                chunks.append(ch)
        ResumeChunk.objects.bulk_create(chunks)
        for lg in list(AgentLog.objects.filter(candidate__owner=src)):
            if lg.candidate_id in cands:
                clone(lg, candidate=cands[lg.candidate_id])


def _cleanup_old_demos():
    """Safety net for tabs that were killed without a sign-out: demo accounts vanish after 2 hours."""
    old = timezone.now() - timedelta(hours=2)
    User.objects.filter(username__startswith=TEMP_PREFIX, date_joined__lt=old).delete()


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([DemoThrottle])
def demo_login(request):
    """One-click demo: a fresh private workspace with sample data. It is deleted on sign-out / page close."""
    _cleanup_old_demos()
    cap = int(getattr(settings, 'MAX_ACTIVE_DEMOS', 40))
    if User.objects.filter(username__startswith=TEMP_PREFIX).count() >= cap:
        return Response({'detail': 'The demo is busy right now. Please try again in a few minutes.'},
                        status=503)
    user = User(username=TEMP_PREFIX + secrets.token_hex(5))
    user.set_unusable_password()
    user.save()
    RecruiterProfile.objects.create(user=user)
    _clone_demo_data(user)
    return Response(_tokens(user))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def demo_end(request):
    """Called on sign-out / page close. Only demo sessions can be removed this way."""
    if not request.user.username.startswith(TEMP_PREFIX):
        return Response({'detail': 'Not a demo session.'}, status=403)
    request.user.delete()
    return Response(status=204)


# ---------- profile ----------
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def profile(request):
    u = request.user
    cands = Candidate.objects.filter(owner=u)
    return Response({
        'username': u.username,
        'email': u.email,
        'date_joined': u.date_joined,
        'is_demo': u.username.startswith(TEMP_PREFIX),
        'jobs': Job.objects.filter(owner=u).count(),
        'candidates': cands.count(),
        'applications': Application.objects.filter(candidate__owner=u).count(),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def clear_data(request):
    """Delete all of the recruiter's own jobs and candidates (the account stays)."""
    if request.user.username == DEMO_USERNAME:
        return Response({'detail': 'The template workspace cannot be changed.'}, status=403)
    with transaction.atomic():
        Candidate.objects.filter(owner=request.user).delete()   # also removes chunks, applications, logs
        Job.objects.filter(owner=request.user).delete()
    return Response({'detail': 'All your jobs and candidates were deleted.'})


# ---------- forgot password (code by email) ----------
class ForgotThrottle(AnonRateThrottle):
    scope = 'forgot'
    rate = '10/hour'          # per IP address


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    code = serializers.RegexField(r'^\d{6}$', error_messages={'invalid': 'Enter the 6-digit code.'})
    new_password = serializers.CharField(max_length=128, trim_whitespace=False)


def _active_user_by_email(email):
    return (User.objects.filter(email__iexact=str(email).strip(), is_active=True)
            .exclude(username__startswith=TEMP_PREFIX).order_by('id').first())


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ForgotThrottle])
def forgot_password(request):
    """Emails a 6-digit reset code. The answer is always the same, so nobody can find out
    which email addresses have an account."""
    import logging
    email = str(request.data.get('email', '')).strip()
    if '@' not in email:
        return Response({'detail': 'Enter the email of your account.'}, status=400)
    reply = Response({'detail': 'If an account with this email exists, a reset code was sent.'})

    user = _active_user_by_email(email)
    if user is None or user.username == DEMO_USERNAME:
        return reply
    otp = EmailOTP.objects.filter(user=user).first()
    if otp is not None and (timezone.now() - otp.last_sent_at).total_seconds() < _otp_cooldown():
        return reply                                     # asked again too quickly: no second email
    try:
        _issue_otp(user, purpose='reset')
    except Exception:
        logging.getLogger(__name__).exception('Could not send the password reset email')
    return reply


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([OtpThrottle])
def reset_password(request):
    """Checks the emailed code and sets the new password."""
    serializer = ResetPasswordSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    invalid = Response({'detail': 'Invalid or expired code.'}, status=400)

    with transaction.atomic():
        user = _active_user_by_email(data['email'])
        otp = EmailOTP.objects.select_for_update().filter(user=user).first() if user else None
        if otp is None or otp.expires_at < timezone.now() or otp.attempts >= _otp_max_attempts():
            return invalid
        if not hmac.compare_digest(otp.code_hash, _hash_code(otp.user_id, data['code'])):
            otp.attempts += 1
            otp.save(update_fields=['attempts'])
            return invalid
        try:
            validate_password(data['new_password'], user=user)
        except DjangoValidationError as e:
            return Response({'new_password': list(e.messages)}, status=400)
        user.set_password(data['new_password'])
        user.save(update_fields=['password'])
        otp.delete()
    return Response({'detail': 'Password changed. You can sign in now.'})