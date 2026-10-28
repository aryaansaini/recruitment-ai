FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Collect admin/static files into /app/staticfiles (served by WhiteNoise).
# SECRET_KEY here is a throw-away value used only during the build.
RUN SECRET_KEY=build-only-not-a-real-secret python manage.py collectstatic --noinput

RUN useradd --create-home app && chown -R app /app
USER app

# Render sets $PORT (10000 by default).
# migrate runs on every start (free instances have no separate release step).
# 1 worker + threads: low memory on the free plan; timeout 120 because the pipeline waits for Gemini.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn recruitment_project.wsgi:application --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 120 --access-logfile -"]