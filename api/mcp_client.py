"""A small client for the project's MCP servers (api/mcp_servers/), built on the official MCP SDK.

`call_tool` starts the server as a child process over stdio, calls one tool, and shuts it down again. The
child gets the parent's environment (database, secret key, ...) plus MCP_OWNER_ID, so an ATS or calendar
server only ever sees the one recruiter it was started for.

Errors are split in two: `McpError` means the server could not be used (not started, crashed, timed out):
callers may fall back to doing the work in-process. `McpToolError` means the server was reached and the
tool refused the request (unknown candidate, bad status, ...): that is a bug in the caller, not an outage.
"""
import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from asgiref.sync import async_to_sync
from django.conf import settings
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

SERVERS = {
    'ats': 'api.mcp_servers.ats_server',
    'resume_parser': 'api.mcp_servers.resume_parser_server',
    'calendar': 'api.mcp_servers.calendar_server',
}


class McpError(Exception):
    """The MCP server could not be used."""


class McpToolError(McpError):
    """The MCP server was reached, but the tool reported an error."""


def child_env(owner_id) -> dict:
    env = {**os.environ, 'PYTHONUNBUFFERED': '1',
           'DJANGO_SETTINGS_MODULE': os.environ.get('DJANGO_SETTINGS_MODULE', 'recruitment_project.settings')}
    env.pop('MCP_OWNER_ID', None)
    if owner_id is not None:
        env['MCP_OWNER_ID'] = str(int(owner_id))
    return env


@asynccontextmanager
async def open_session(server: str, owner_id=None):
    """A connected MCP client session to a server started over stdio. (Tests swap this for an in-memory one.)"""
    params = StdioServerParameters(command=sys.executable, args=['-m', SERVERS[server]],
                                   env=child_env(owner_id), cwd=str(settings.BASE_DIR))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def unwrap(result):
    """The value of a tool result: structured content if there is any, else the text (parsed as JSON if it is)."""
    texts = [c.text for c in result.content if getattr(c, 'type', '') == 'text']
    if result.isError:
        raise McpToolError((texts[0] if texts else 'The tool failed.').removeprefix('Error executing tool ').strip())
    structured = result.structuredContent
    if structured is not None:
        return structured['result'] if set(structured) == {'result'} else structured
    if not texts:
        return None
    try:
        return json.loads(texts[0])
    except ValueError:
        return texts[0]


async def _call(server, tool, arguments, owner_id, timeout):
    async def run():
        async with open_session(server, owner_id) as session:
            return await session.call_tool(tool, arguments)
    return await asyncio.wait_for(run(), timeout)


def _run_sync(coroutine_function, *args):
    try:
        asyncio.get_running_loop()
    except RuntimeError:                       # normal case: plain sync code (Django views, pipeline steps)
        return async_to_sync(coroutine_function)(*args)
    with ThreadPoolExecutor(max_workers=1) as pool:     # called from a thread that already runs an event loop
        return pool.submit(asyncio.run, coroutine_function(*args)).result()


def call_tool(server: str, tool: str, arguments: dict | None = None, owner_id=None, timeout=None):
    """Call one tool of one of the project's MCP servers and return its value."""
    if server not in SERVERS:
        raise McpError(f'Unknown MCP server: {server}')
    timeout = timeout or getattr(settings, 'MCP_TIMEOUT_SECONDS', 30)
    try:
        result = _run_sync(_call, server, tool, arguments or {}, owner_id, timeout)
    except BaseException as exc:               # includes anyio's ExceptionGroup and timeouts
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise McpError(f'{server}.{tool} failed: {type(exc).__name__}: {str(exc)[:200]}') from exc
    return unwrap(result)
