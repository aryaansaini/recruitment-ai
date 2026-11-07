"""Calendar MCP server: interview slots for ONE recruiter (MCP_OWNER_ID).

Tools
    propose_interview_slot(candidate_name)   the pipeline's slot: three days from now, with a meeting link
    find_free_slots(count?, days_ahead?)     next free weekday slots (10:00 to 16:00, hourly), skipping booked ones
    list_booked_slots(days_ahead?)           interviews already scheduled

Times are UTC. This is not a connection to Google Calendar; it works on the interview times stored in the app.

Run:  MCP_OWNER_ID=3 python -m api.mcp_servers.calendar_server
"""
from datetime import datetime, timedelta
from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP

from ._common import run_stdio

PLATFORM = 'Google Meet'
START_HOURS = range(10, 17)            # interviews start at 10:00, 11:00 ... 16:00


def _slot(when, candidate_name=None) -> dict:
    slot = {'slot': when.strftime('%Y-%m-%d %H:%M'), 'datetime': when.isoformat()}
    if candidate_name is not None:
        slot = {'candidate': candidate_name, **slot, 'platform': PLATFORM,
                'link': 'meet.google.com/recruitment-' + candidate_name.lower().replace(' ', '-')}
    return slot


def propose_slot(candidate_name: str, now=None) -> dict:
    from django.utils import timezone
    when = ((now or timezone.now()) + timedelta(days=3)).replace(second=0, microsecond=0)
    return _slot(when, candidate_name)


def booked_between(owner_id: int, start, end) -> list:
    from api.models import Candidate
    return list(Candidate.objects.filter(owner_id=owner_id, interview_date__gte=start, interview_date__lt=end)
                .order_by('interview_date'))


def free_slots(owner_id: int, count: int = 3, days_ahead: int = 14, now=None) -> list:
    from django.utils import timezone
    count, days_ahead = max(1, min(int(count), 20)), max(1, min(int(days_ahead), 60))
    today = timezone.localtime(now or timezone.now()).date()
    start = timezone.make_aware(datetime.combine(today + timedelta(days=1), datetime.min.time()))
    taken = {(timezone.localtime(c.interview_date).date(), timezone.localtime(c.interview_date).hour)
             for c in booked_between(owner_id, start, start + timedelta(days=days_ahead))}
    found = []
    for offset in range(1, days_ahead + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:                                  # Saturday, Sunday
            continue
        for hour in START_HOURS:
            if (day, hour) in taken:
                continue
            found.append(_slot(timezone.make_aware(datetime(day.year, day.month, day.day, hour))))
            if len(found) == count:
                return found
    return found


def booked_slots(owner_id: int, days_ahead: int = 14, now=None) -> list:
    from django.utils import timezone
    start = now or timezone.now()
    days_ahead = max(1, min(int(days_ahead), 60))
    return [{'candidate_id': c.pk, 'candidate': c.name, **_slot(c.interview_date)}
            for c in booked_between(owner_id, start, start + timedelta(days=days_ahead))]


def create_server(owner_id: int) -> FastMCP:
    server = FastMCP('recruit-calendar', instructions='Interview calendar of one recruiter. Times are UTC.')

    @server.tool()
    async def propose_interview_slot(candidate_name: str) -> dict[str, Any]:
        """Propose an interview three days from now for a candidate: slot, ISO datetime, platform, meeting link."""
        return await sync_to_async(propose_slot)(candidate_name)

    @server.tool()
    async def find_free_slots(count: int = 3, days_ahead: int = 14) -> list[dict[str, Any]]:
        """Find the next free interview slots on weekdays between 10:00 and 16:00 (hourly), skipping times
        that already have an interview. At most 20 slots, looking at most 60 days ahead."""
        return await sync_to_async(free_slots)(owner_id, count, days_ahead)

    @server.tool()
    async def list_booked_slots(days_ahead: int = 14) -> list[dict[str, Any]]:
        """List the interviews already scheduled in the next days (at most 60)."""
        return await sync_to_async(booked_slots)(owner_id, days_ahead)

    return server


def main():
    run_stdio(create_server, needs_owner=True)


if __name__ == '__main__':
    main()
