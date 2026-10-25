from django.core.management.base import BaseCommand, CommandError

from api.embeddings import BATCH_SIZE, EmbeddingError, embed_texts
from api.models import ResumeChunk


class Command(BaseCommand):
    help = 'Create embeddings for resume chunks that have none (use --all to redo every chunk)'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Re-embed every chunk (e.g. after changing the model)')

    def handle(self, *args, **options):
        qs = ResumeChunk.objects.all() if options['all'] else ResumeChunk.objects.filter(embedding__isnull=True)
        ids = list(qs.order_by('id').values_list('id', flat=True))
        if not ids:
            self.stdout.write('Nothing to embed: every chunk already has an embedding.')
            return
        done = 0
        for i in range(0, len(ids), BATCH_SIZE):
            batch = list(ResumeChunk.objects.filter(id__in=ids[i:i + BATCH_SIZE]))
            try:
                vectors = embed_texts([c.embed_text for c in batch])
            except EmbeddingError as exc:
                raise CommandError(f'Stopped after {done}/{len(ids)} chunks: {exc}')
            for chunk, vector in zip(batch, vectors):
                chunk.embedding = vector
            ResumeChunk.objects.bulk_update(batch, ['embedding'])
            done += len(batch)
            self.stdout.write(f'  embedded {done}/{len(ids)}')
        self.stdout.write(self.style.SUCCESS(f'Done. {done} chunks embedded.'))