from django.db import transaction

from .models import Candidate, ResumeChunk
from .resume_parser import chunk_resume


def rebuild_resume_chunks(candidate: Candidate) -> int:
    """(Re)create the resume chunks of a candidate. Returns how many were stored."""
    chunks = chunk_resume(candidate.resume_text)
    with transaction.atomic():
        candidate.chunks.all().delete()
        ResumeChunk.objects.bulk_create([
            ResumeChunk(candidate=candidate, section=c.section, chunk_index=c.index, text=c.text)
            for c in chunks
        ])
    return len(chunks)