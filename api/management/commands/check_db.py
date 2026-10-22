from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = 'Verify database connection and pgvector support'

    def handle(self, *args, **options):
        self.stdout.write(f'Database vendor: {connection.vendor}')
        with connection.cursor() as cur:
            if connection.vendor != 'postgresql':
                self.stdout.write(self.style.WARNING(
                    'Not Postgres (SQLite fallback). Set DATABASE_URL in .env to use Neon.'))
                return
            cur.execute('SELECT version()')
            self.stdout.write(cur.fetchone()[0])
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            if not row:
                self.stdout.write(self.style.ERROR('pgvector extension NOT enabled. Run: python manage.py migrate'))
                return
            self.stdout.write(self.style.SUCCESS(f'pgvector enabled (version {row[0]})'))
            cur.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector")
            self.stdout.write(self.style.SUCCESS(f'Vector distance test OK: {cur.fetchone()[0]}'))