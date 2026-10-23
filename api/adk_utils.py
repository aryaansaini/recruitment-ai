"""
Helpers to run Google ADK agents from normal (synchronous) Django code.

ADK is async; Django views here are sync, so we bridge with asgiref's
async_to_sync. Each call uses a fresh in-memory session.
"""
import uuid

from asgiref.sync import async_to_sync
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

APP_NAME = 'recruitment_ai'


async def _run_agent(agent, message: str, user_id: str, state: dict | None):
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=str(uuid.uuid4()), state=state or {},
    )
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)

    final_text = None
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role='user', parts=[types.Part(text=message)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = ''.join(part.text or '' for part in event.content.parts)

    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session.id,
    )
    return final_text, dict(session.state)


def run_agent_sync(agent, message: str, *, user_id: str = 'recruiter', state: dict | None = None):
    """Run an ADK agent to completion. Returns (final_text, session_state)."""
    return async_to_sync(_run_agent)(agent, message, user_id, state)