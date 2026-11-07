"""Resume parser MCP server: turn resume files and text into structured data. No database, no owner.

Tools
    extract_resume_text(filename, content_base64)   PDF, DOCX or TXT file -> clean text
    analyze_resume(text)                            word count, tech keywords, email, phone
    chunk_resume_text(text, max_chars?)             section-aware chunks

Run:  python -m api.mcp_servers.resume_parser_server
"""
import base64
import binascii
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP

from api.resume_parser import (MAX_RESUME_BYTES, MAX_TEXT_CHARS, ResumeParseError, chunk_resume, extract_email,
                               extract_phone, extract_text, keyword_summary)

from ._common import run_stdio


def create_server() -> FastMCP:
    server = FastMCP('recruit-resume-parser',
                     instructions='Parses resumes. Files are sent base64-encoded, at most 5 MB.')

    @server.tool()
    def extract_resume_text(filename: str, content_base64: str) -> str:
        """Extract clean text from a resume file. filename decides the type (.pdf, .docx or .txt);
        content_base64 is the file content, base64-encoded."""
        if len(content_base64) > MAX_RESUME_BYTES * 4 // 3 + 8:           # check before decoding
            raise ValueError('File is larger than 5 MB.')
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('content_base64 is not valid base64.') from None
        try:
            return extract_text(data, filename)
        except ResumeParseError as exc:
            raise ValueError(str(exc)) from None

    @server.tool()
    def analyze_resume(text: str) -> dict[str, Any]:
        """Summarise resume text: word count, which common tech keywords appear, and the first email and
        phone number found."""
        if not text.strip():
            raise ValueError('text is empty.')
        text = text[:MAX_TEXT_CHARS]
        return {**keyword_summary(text), 'email': extract_email(text), 'phone': extract_phone(text)}

    @server.tool()
    def chunk_resume_text(text: str, max_chars: int = 700) -> list[dict[str, Any]]:
        """Split resume text into section-aware chunks (section, index, text). The header with contact
        details is left out on purpose."""
        if not text.strip():
            raise ValueError('text is empty.')
        return [asdict(c) for c in chunk_resume(text[:MAX_TEXT_CHARS], max_chars=max(200, min(int(max_chars), 2000)))]

    return server


def main():
    run_stdio(create_server, needs_owner=False)


if __name__ == '__main__':
    main()
