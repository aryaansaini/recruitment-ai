"""Shared start-up code of the MCP servers (kept free of any MCP import so it is cheap to import)."""
import os


def setup_django():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'recruitment_project.settings')
    import django
    django.setup()


def owner_id_from_env() -> int:
    """The recruiter this server instance works for. Refuses to start without one: a server that could see
    every recruiter's candidates would bypass the isolation the API enforces."""
    raw = os.environ.get('MCP_OWNER_ID', '').strip()
    if not raw.isdigit():
        raise SystemExit('MCP_OWNER_ID must be set to the id of the recruiter this server works for.')
    return int(raw)


def run_stdio(create_server, needs_owner: bool):
    """Entry point of a server module: log to stderr only (stdout is the protocol channel) and serve stdio."""
    owner_id = owner_id_from_env() if needs_owner else None
    if needs_owner:
        setup_django()
    server = create_server(owner_id) if needs_owner else create_server()
    server.run(transport='stdio')
