"""Interview management: schedule, free slots, reschedule, cancel, complete, and the e-mails that go with it.

An `Interview` belongs to one `Application`. The scheduling agent proposes a time; `sync_from_pipeline`
turns it into an Interview (moving it to the next free slot when that time is already taken). After that the
recruiter manages it from the Interviews page.

Times are stored in UTC. Free slots are weekdays, 10:00 to 16:00, one hour each (the same grid as the
calendar MCP server). A recruiter can still pick any future time by hand: only an overlap with another
active interview is refused.
"""
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import AgentLog, Application, Candidate, Interview

logger = logging.getLogger(__name__)

SLOT_HOURS = range(10, 17)
DEFAULT_DURATION = 60
MAX_DURATION = 240
MAX_DAYS_AHEAD = 60
TEMP_DEMO_PREFIX = 'demo_session_'      # throw-away demo accounts never send e-mail (see auth_views.TEMP_PREFIX)


class InterviewError(Exception):
    """A request the recruiter can fix: the message is shown to them. `status` is the HTTP status to answer."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def _aware(value):
    if timezone.is_naive(value):
        return timezone.make_aware(value)
    return value


def _end(interview_or_start, minutes=None):
    if isinstance(interview_or_start, Interview):
        return interview_or_start.scheduled_at + timedelta(minutes=interview_or_start.duration_minutes)
    return interview_or_start + timedelta(minutes=minutes)


def meeting_link(name: str) -> str:
    return 'meet.google.com/recruitment-' + str(name).lower().replace(' ', '-')


def _active(owner):
    return Interview.objects.filter(owner=owner, status__in=Interview.ACTIVE)


def conflicting(owner, start, minutes=DEFAULT_DURATION, exclude_application_id=None):
    """The active interviews of this recruiter that overlap [start, start + minutes)."""
    end = _end(start, minutes)
    window = _active(owner).filter(scheduled_at__lt=end,
                                   scheduled_at__gt=start - timedelta(minutes=MAX_DURATION))
    if exclude_application_id is not None:
        window = window.exclude(application_id=exclude_application_id)
    return [i for i in window.select_related('candidate') if _end(i) > start]


def available_slots(owner, days=7, start_date=None, exclude_application_id=None, after=None) -> list:
    """Free hourly slots on weekdays, in the future, for `days` days from `start_date` (default: today)."""
    days = max(1, min(int(days), MAX_DAYS_AHEAD))
    now = timezone.now()
    first = start_date or timezone.localtime(now).date()
    after = max(after, now) if after else now
    window_start = _aware(datetime.combine(first, datetime.min.time()))
    window_end = window_start + timedelta(days=days)
    taken = _active(owner).filter(scheduled_at__gte=window_start - timedelta(minutes=MAX_DURATION),
                                  scheduled_at__lt=window_end)
    if exclude_application_id is not None:
        taken = taken.exclude(application_id=exclude_application_id)
    busy = [(i.scheduled_at, _end(i)) for i in taken]

    slots = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        if day.weekday() >= 5:                                   # Saturday, Sunday
            continue
        for hour in SLOT_HOURS:
            start = _aware(datetime(day.year, day.month, day.day, hour))
            end = _end(start, DEFAULT_DURATION)
            if start <= after or any(b_start < end and b_end > start for b_start, b_end in busy):
                continue
            slots.append({'start': start.isoformat(), 'date': start.strftime('%Y-%m-%d'),
                          'time': start.strftime('%H:%M')})
    return slots


def next_free_slot(owner, after, exclude_application_id=None):
    """The first free standard slot after `after` (None if nothing is free in the next 60 days)."""
    slots = available_slots(owner, days=MAX_DAYS_AHEAD, start_date=timezone.localtime(_aware(after)).date(),
                            exclude_application_id=exclude_application_id, after=after)
    return datetime.fromisoformat(slots[0]['start']) if slots else None


# --------------------------------------------------------------------------
# E-mail notifications
# --------------------------------------------------------------------------
SUBJECTS = {
    'scheduled': 'Interview scheduled: {job}',
    'rescheduled': 'Interview rescheduled: {job}',
    'cancelled': 'Interview cancelled: {job}',
}
INTRO = {
    'scheduled': 'Your interview for {job} has been scheduled.',
    'rescheduled': 'Your interview for {job} has been moved to a new time.',
    'cancelled': 'Your interview for {job} has been cancelled.',
}


def _email_allowed(owner) -> bool:
    if not getattr(settings, 'INTERVIEW_EMAILS_ENABLED', True):
        return False
    if owner.username.startswith(TEMP_DEMO_PREFIX) or owner.username == 'demo_recruiter':
        return False
    limit = getattr(settings, 'INTERVIEW_EMAILS_PER_DAY', 40)
    key = f'interview-mail:{owner.pk}:{timezone.now():%Y%m%d}'
    cache.add(key, 0, 86400)
    try:
        return cache.incr(key) <= limit
    except ValueError:
        return True


def _compose(interview, event, detail):
    when = timezone.localtime(interview.scheduled_at).strftime('%A, %d %B %Y at %H:%M %Z')
    job = interview.job.title
    lines = [INTRO[event].format(job=job), '']
    if event == 'cancelled':
        if detail:
            lines.append(f'Reason: {detail}')
        lines.append('We will contact you if a new time is arranged.')
    else:
        lines.append(f'When: {when}')
        lines.append(f'Duration: {interview.duration_minutes} minutes')
        lines.append(f'Where: {interview.platform}' + (f' ({interview.link})' if interview.link else ''))
        if event == 'rescheduled' and detail:
            lines.append(f'Note: {detail}')
    return SUBJECTS[event].format(job=job), '\n'.join(lines)


def notify(interview, event, detail='') -> list:
    """E-mail the candidate (and a copy to the recruiter). Never raises. Returns what happened per address."""
    owner = interview.owner
    results = []
    if not _email_allowed(owner):
        return [{'to': interview.candidate.email, 'sent': False, 'error': 'E-mail is switched off for this account.'}]
    subject, body = _compose(interview, event, detail)
    recipients = [(interview.candidate.email, body)]
    if owner.email:
        recipients.append((owner.email, f'Copy for the recruiter. Candidate: {interview.candidate.name}\n\n{body}'))
    for address, text in recipients:
        try:
            send_mail(subject, text, settings.DEFAULT_FROM_EMAIL, [address], fail_silently=False)
            results.append({'to': address, 'sent': True, 'error': ''})
        except Exception as exc:                          # a mail problem must never undo the interview change
            logger.warning('Interview e-mail to %s failed: %s', address, exc)
            results.append({'to': address, 'sent': False, 'error': f'{type(exc).__name__}: {str(exc)[:150]}'})
    return results


def _record(interview, event, detail='', emails=None):
    interview.history = (interview.history or []) + [{
        'at': timezone.now().isoformat(), 'event': event, 'detail': detail[:300],
        'scheduled_at': interview.scheduled_at.isoformat(), 'email': emails or [],
    }]


def _log(candidate, from_agent, message):
    AgentLog.objects.create(candidate=candidate, from_agent=from_agent, to_agent='Interview calendar', message=message[:2000])


# --------------------------------------------------------------------------
# Keeping the candidate and the application in step with their interview
# --------------------------------------------------------------------------
def _sync_records(interview):
    app, candidate = interview.application, interview.candidate
    if interview.status in Interview.ACTIVE:
        app.status = Application.Status.INTERVIEW_SCHEDULED
        app.interview_date = interview.scheduled_at
        candidate.status = Candidate.Status.INTERVIEW_SCHEDULED
        candidate.interview_date = interview.scheduled_at
    elif interview.status == Interview.Status.CANCELLED:
        app.status = Application.Status.RANKED               # qualified, but no interview any more
        app.interview_date = None
        nxt = (_active(interview.owner).filter(candidate=candidate, scheduled_at__gt=timezone.now())
               .exclude(pk=interview.pk).order_by('scheduled_at').first())
        candidate.interview_date = nxt.scheduled_at if nxt else None
        candidate.status = Candidate.Status.INTERVIEW_SCHEDULED if nxt else Candidate.Status.RANKED
    app.save(update_fields=['status', 'interview_date', 'updated_at'])
    candidate.save(update_fields=['status', 'interview_date'])


def _validate_time(owner, when, duration, application_id, must_be_future=True):
    when = _aware(when)
    if must_be_future and when <= timezone.now():
        raise InterviewError('Pick a time in the future.')
    if when > timezone.now() + timedelta(days=365):
        raise InterviewError('Pick a time within the next year.')
    if not 15 <= duration <= MAX_DURATION:
        raise InterviewError(f'Duration must be between 15 and {MAX_DURATION} minutes.')
    clash = conflicting(owner, when, duration, exclude_application_id=application_id)
    if clash:
        c = clash[0]
        raise InterviewError(f'That time overlaps with the interview of {c.candidate.name} '
                             f'({timezone.localtime(c.scheduled_at):%H:%M}). Pick another slot.', status=409)
    return when


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
@transaction.atomic
def schedule(application, when, *, duration=DEFAULT_DURATION, notes='', platform='Google Meet', link='',
             notify_people=True, actor='Recruiter'):
    """Schedule (or re-schedule after a cancellation) the interview of an application."""
    owner = application.candidate.owner
    if owner is None:
        raise InterviewError('This candidate has no owner.')
    existing = Interview.objects.filter(application=application).first()
    if existing and existing.status in Interview.ACTIVE:
        raise InterviewError('This application already has an interview. Reschedule it instead.', status=409)
    when = _validate_time(owner, when, duration, application.pk)
    values = {'owner': owner, 'candidate': application.candidate, 'job': application.job, 'scheduled_at': when,
              'duration_minutes': duration, 'notes': notes[:1000], 'platform': platform,
              'link': link or meeting_link(application.candidate.name), 'status': Interview.Status.SCHEDULED}
    if existing:
        for k, v in values.items():
            setattr(existing, k, v)
        interview = existing
    else:
        interview = Interview(application=application, **values)
    interview.save()
    _sync_records(interview)
    emails = notify(interview, 'scheduled') if notify_people else []
    _record(interview, 'scheduled', f'Scheduled by {actor}.', emails)
    interview.save(update_fields=['history', 'updated_at'])
    _log(application.candidate, actor, f'Interview scheduled for {application.job.title} on '
                                       f'{timezone.localtime(when):%Y-%m-%d %H:%M}.')
    return interview


@transaction.atomic
def reschedule(interview, when, *, reason='', duration=None, notify_people=True):
    if interview.status == Interview.Status.COMPLETED:
        raise InterviewError('A completed interview cannot be rescheduled.', status=409)
    if interview.status == Interview.Status.CANCELLED:
        raise InterviewError('This interview was cancelled. Schedule a new one from the application.', status=409)
    duration = duration or interview.duration_minutes
    when = _validate_time(interview.owner, when, duration, interview.application_id)
    old = interview.scheduled_at
    interview.scheduled_at, interview.duration_minutes = when, duration
    interview.status = Interview.Status.RESCHEDULED
    interview.save()
    _sync_records(interview)
    emails = notify(interview, 'rescheduled', reason) if notify_people else []
    _record(interview, 'rescheduled', f'Moved from {timezone.localtime(old):%Y-%m-%d %H:%M}. {reason}'.strip(), emails)
    interview.save(update_fields=['history', 'updated_at'])
    _log(interview.candidate, 'Recruiter', f'Interview for {interview.job.title} moved to '
                                           f'{timezone.localtime(when):%Y-%m-%d %H:%M}.')
    return interview


@transaction.atomic
def cancel(interview, *, reason='', notify_people=True, sync_records=True):
    if interview.status == Interview.Status.COMPLETED:
        raise InterviewError('A completed interview cannot be cancelled.', status=409)
    if interview.status == Interview.Status.CANCELLED:
        raise InterviewError('This interview is already cancelled.', status=409)
    interview.status = Interview.Status.CANCELLED
    interview.save()
    if sync_records:                                   # the pipeline sets the statuses itself when it cancels
        _sync_records(interview)
    emails = notify(interview, 'cancelled', reason) if notify_people else []
    _record(interview, 'cancelled', reason or 'Cancelled.', emails)
    interview.save(update_fields=['history', 'updated_at'])
    _log(interview.candidate, 'Recruiter', f'Interview for {interview.job.title} cancelled.' + (f' {reason}' if reason else ''))
    return interview


@transaction.atomic
def complete(interview, *, notes=''):
    if interview.status not in Interview.ACTIVE:
        raise InterviewError('Only a scheduled interview can be marked as completed.', status=409)
    interview.status = Interview.Status.COMPLETED
    if notes:
        interview.notes = notes[:1000]
    _record(interview, 'completed', notes or 'Marked as completed.')
    interview.save()
    _log(interview.candidate, 'Recruiter', f'Interview for {interview.job.title} marked as completed.')
    return interview


# --------------------------------------------------------------------------
# The pipeline's scheduling agent
# --------------------------------------------------------------------------
def sync_from_pipeline(application, slot=None):
    """Called at the end of a pipeline run. Qualified: the proposed time becomes an Interview (moved to the next
    free slot if taken). Not qualified any more: an interview from an earlier run is cancelled."""
    candidate = application.candidate
    owner = candidate.owner
    if owner is None:
        return None
    existing = Interview.objects.filter(application=application).first()

    if application.status != Application.Status.INTERVIEW_SCHEDULED or not application.interview_date:
        if existing and existing.status in Interview.ACTIVE:
            cancel(existing, reason='The candidate no longer qualifies for this job after a new evaluation.',
                   sync_records=False)
        return None

    when = _aware(application.interview_date)
    moved = False
    if conflicting(owner, when, DEFAULT_DURATION, exclude_application_id=application.pk):
        free = next_free_slot(owner, when, exclude_application_id=application.pk)
        if free is not None:
            when, moved = free, True
    slot = slot or {}
    with transaction.atomic():
        values = {'owner': owner, 'candidate': candidate, 'job': application.job, 'scheduled_at': when,
                  'duration_minutes': DEFAULT_DURATION, 'platform': slot.get('platform', 'Google Meet'),
                  'link': slot.get('link', meeting_link(candidate.name)), 'status': Interview.Status.SCHEDULED}
        if existing:
            for k, v in values.items():
                setattr(existing, k, v)
            interview = existing
        else:
            interview = Interview(application=application, **values)
        interview.save()
        _sync_records(interview)
        if moved:
            _log(candidate, 'Interview Coordination Agent',
                 f'The proposed time was already booked, so the interview was moved to {timezone.localtime(when):%Y-%m-%d %H:%M}.')
    emails = notify(interview, 'scheduled')
    _record(interview, 'scheduled', 'Scheduled by the Interview Coordination Agent.', emails)
    interview.save(update_fields=['history', 'updated_at'])
    return interview


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def serialize(interview) -> dict:
    return {
        'id': interview.pk, 'application_id': interview.application_id,
        'candidate_id': interview.candidate_id, 'candidate_name': interview.candidate.name,
        'candidate_email': interview.candidate.email,
        'job_id': interview.job_id, 'job_title': interview.job.title,
        'scheduled_at': interview.scheduled_at, 'duration_minutes': interview.duration_minutes,
        'platform': interview.platform, 'link': interview.link, 'status': interview.status,
        'notes': interview.notes, 'history': interview.history or [], 'updated_at': interview.updated_at,
    }
