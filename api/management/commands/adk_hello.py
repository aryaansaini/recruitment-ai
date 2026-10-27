from django.core.management.base import BaseCommand, CommandError

from api.adk_agents import extract_skills

SAMPLE = (
    'Aryan Saini. Backend developer with 3 years of experience building REST APIs '
    'using Python, Django and PostgreSQL. Deployed apps with Docker. Basic React.'
)


class Command(BaseCommand):
    help = 'Smoke test: run a real ADK agent (Gemini) from Django and print its structured output'

    def add_arguments(self, parser):
        parser.add_argument('--text', default=SAMPLE, help='Resume text to analyse')

    def handle(self, *args, **options):
        try:
            profile = extract_skills(options['text'])
        except Exception as exc:
            raise CommandError(f'ADK run failed: {exc}')
        self.stdout.write(self.style.SUCCESS('ADK agent ran successfully'))
        self.stdout.write(profile.model_dump_json(indent=2))