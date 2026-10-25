import logging

from django.db import transaction

from .embeddings import EmbeddingError, embed_texts
from .models import Candidate, ResumeChunk
from .resume_parser import chunk_resume

logger = logging.getLogger(__name__)


def rebuild_resume_chunks(candidate: Candidate) -> int:
    """(Re)create the resume chunks of a candidate (without embeddings). Returns how many were stored."""
    chunks = chunk_resume(candidate.resume_text)
    with transaction.atomic():
        candidate.chunks.all().delete()
        ResumeChunk.objects.bulk_create([
            ResumeChunk(candidate=candidate, section=c.section, chunk_index=c.index, text=c.text)
            for c in chunks
        ])
    return len(chunks)


def embed_candidate_chunks(candidate: Candidate, only_missing: bool = True) -> int:
    """
    Embed the candidate's chunks with Gemini and store the vectors in pgvector.
    Never raises: if the embedding service fails the chunks simply stay un-embedded
    (retry later with `manage.py embed_chunks`). Returns how many chunks were embedded.
    """
    qs = candidate.chunks.filter(embedding__isnull=True) if only_missing else candidate.chunks.all()
    chunks = list(qs)
    if not chunks:
        return 0
    try:
        vectors = embed_texts([c.embed_text for c in chunks])
    except EmbeddingError as exc:
        logger.warning('Embedding failed for candidate %s: %s', candidate.pk, exc)
        return 0
    for chunk, vector in zip(chunks, vectors):
        chunk.embedding = vector
    ResumeChunk.objects.bulk_update(chunks, ['embedding'])
    return len(chunks)