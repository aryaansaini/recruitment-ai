import os
import sys
from datetime import timedelta
import dj_database_url
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')


# ---------- Core / Security ----------
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError('SECRET_KEY environment variable is not set (see .env.example)')

DEBUG = os.getenv('DEBUG', 'False') == 'True'

# Render sets RENDER_EXTERNAL_HOSTNAME (e.g. my-app.onrender.com) automatically
RENDER_HOST = os.getenv('RENDER_EXTERNAL_HOSTNAME', '')

ALLOWED_HOSTS = [
    h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()
] + ([RENDER_HOST] if RENDER_HOST else [])

# Needed for /admin login over HTTPS behind Render's proxy
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
] + ([f'https://{RENDER_HOST}'] if RENDER_HOST else [])

CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()
]

# ---------- Gemini / ADK ----------
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
# Google ADK / google-genai read the key from GOOGLE_API_KEY
if GEMINI_API_KEY:
    os.environ.setdefault('GOOGLE_API_KEY', GEMINI_API_KEY)
EMBEDDING_MODEL = os.getenv('EMBEDDING_MODEL', 'gemini-embedding-001')
EMBEDDING_DIM = 768   # must match ResumeChunk.embedding dimensions (changing it needs a migration)

# ---------- Sign-ups and per-user limits ----------
SIGNUPS_ENABLED = os.getenv('SIGNUPS_ENABLED', 'True') == 'True'      # False = nobody can register
SIGNUP_INVITE_CODE = os.getenv('SIGNUP_INVITE_CODE', '')              # set it to make sign-up invite-only
MAX_SIGNUPS_PER_DAY = int(os.getenv('MAX_SIGNUPS_PER_DAY', '30'))     # all new accounts together, per 24 h
MAX_CANDIDATES_PER_USER = int(os.getenv('MAX_CANDIDATES_PER_USER', '20'))   # staff accounts are exempt
MAX_JOBS_PER_USER = int(os.getenv('MAX_JOBS_PER_USER', '5'))

# ---------- Application definition ----------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'api',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',   # serves static files (admin CSS/JS) from gunicorn
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'recruitment_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'recruitment_project.wsgi.application'


# ---------- Database ----------
# Normal use  : DATABASE_URL set   -> Postgres (Neon, with pgvector); empty -> local SQLite.
# `manage.py test` ALWAYS uses SQLite (so tests never touch Neon), unless TEST_DATABASE_URL is set.
RUNNING_TESTS = len(sys.argv) > 1 and sys.argv[1] == 'test'
DATABASE_URL = os.getenv('TEST_DATABASE_URL', '') if RUNNING_TESTS else os.getenv('DATABASE_URL', '')
DATABASES = {
    'default': dj_database_url.parse(
        DATABASE_URL or f'sqlite:///{BASE_DIR / "db.sqlite3"}',  # empty value -> SQLite
        conn_max_age=600,
        conn_health_checks=True,     # Neon closes idle connections: re-check before reusing one
    )
}
# Neon's pooled endpoint (pgbouncer) does not support server-side cursors
DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'] = True


# ---------- Password validation ----------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# ---------- Internationalization ----------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# ---------- Static files ----------
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'      # filled by `collectstatic` during the Docker build
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ---------- REST framework + JWT auth ----------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    # Every API endpoint requires a valid login token unless a view says otherwise
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # Number of proxies in front of Django (Render = 1) so per-IP limits use the real client address
    'NUM_PROXIES': int(os.getenv('NUM_PROXIES', '1')),
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '30/min',
        'user': '120/min',
        'login': '10/min',      # brute-force protection
        'pipeline': '10/min',   # each run calls Gemini
        'search': '30/min',     # each search calls the embedding API
        'pipeline_day': '30/day',
        'search_day': '200/day',
    },
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
}


# ---------- Upload limits ----------
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024   # 5 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024


# ---------- Production hardening (only when DEBUG is off) ----------
# Render terminates HTTPS and tells Django via this header
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_SSL_REDIRECT = not RUNNING_TESTS          # http -> https (tests use plain http)
    SECURE_REDIRECT_EXEMPT = [r'^healthz$']          # health checks must never be redirected
    SECURE_HSTS_SECONDS = 3600                       # raise later once everything works


# ---------- Logging ----------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
}


# ---------- Email (sign-up OTP) ----------
# Local default: emails are printed in the terminal (no real email is sent).
# On Render set EMAIL_BACKEND=api.brevo_backend.BrevoEmailBackend and BREVO_API_KEY
# (Render's free plan blocks the normal SMTP ports).
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'Recruit AI <no-reply@recruit-ai.local>')
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')

# True  = sign-up first emails a 6-digit code and the account is activated after it is entered.
# False = sign-up logs the user in directly. Tests always use False (individual tests override it).
EMAIL_OTP_REQUIRED = False if RUNNING_TESTS else os.environ.get('EMAIL_OTP_REQUIRED', 'False') == 'True'


# ---------- Pipeline ----------
# True  = the four agents run inside a Google ADK SequentialAgent.
# False = the same agents run as plain Python functions.
PIPELINE_USE_ADK = os.environ.get('PIPELINE_USE_ADK', 'True') == 'True'