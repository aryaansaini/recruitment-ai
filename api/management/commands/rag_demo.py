import json

from django.core.management.base import BaseCommand, CommandError

from api import rag
from api.llm import LLMError
from api.models import Candidate, Job


class Command(BaseCommand):
    help = 'Smoke test: RAG-score one candidate for one job with real Gemini and print the score card'

    def add_arguments(self, parser):
        parser.add_argument('--candidate', type=int, required=True)
        parser.add_argument('--job', type=int, required=True)

    def handle(self, *args, **options):
        try:
            candidate = Candidate.objects.get(pk=options['candidate'])
            job = Job.objects.get(pk=options['job'])
        except (Candidate.DoesNotExist, Job.DoesNotExist):
            raise CommandError('Candidate or job not found.')

        evidence = rag.retrieve_evidence(candidate, job)
        self.stdout.write(f"Retrieval: {evidence['mode']} ({len(evidence['chunks'])} chunks)")
        for chunk in evidence['chunks']:
            self.stdout.write(f"  [chunk {chunk['chunk_id']} | {chunk['section']}] similarity={chunk['similarity']}")
        try:
            card = rag.score_candidate(candidate, job, evidence=evidence)
        except LLMError as exc:
            raise CommandError(f'Scoring failed: {exc}')
        self.stdout.write(self.style.SUCCESS('RAG score card:'))
        self.stdout.write(json.dumps(card, indent=2, ensure_ascii=False))