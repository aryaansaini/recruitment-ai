"""MCP servers for the recruitment workspace, built with the official MCP Python SDK (FastMCP).

    ats_server            update and read candidate records (the applicant tracking system)
    resume_parser_server  turn resume files and text into structured data
    calendar_server       propose and look up interview slots

Run one over stdio, for the pipeline or for any MCP client (MCP Inspector, Claude Desktop, ...):

    MCP_OWNER_ID=3 python -m api.mcp_servers.ats_server

The ATS and calendar servers work for ONE recruiter, given by MCP_OWNER_ID, and cannot see anyone else's
candidates. The resume parser server needs no database and no owner.
"""
