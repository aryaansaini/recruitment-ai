from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Send one test email with the current email settings and show why it failed, if it does.'

    def add_arguments(self, parser):
        parser.add_argument('to', help='Email address that should receive the test email')

    def handle(self, *args, **options):
        self.stdout.write(f'EMAIL_BACKEND        = {settings.EMAIL_BACKEND}')
        self.stdout.write(f'DEFAULT_FROM_EMAIL   = {settings.DEFAULT_FROM_EMAIL}')
        self.stdout.write('BREVO_API_KEY set    = ' + ('yes' if getattr(settings, 'BREVO_API_KEY', '') else 'no'))
        self.stdout.write(f'EMAIL_OTP_REQUIRED   = {getattr(settings, "EMAIL_OTP_REQUIRED", False)}')
        try:
            sent = send_mail('Recruit AI test email', 'If you can read this, email sending works.',
                             None, [options['to']], fail_silently=False)
        except Exception as exc:
            raise CommandError(f'Sending failed: {type(exc).__name__}: {exc}')
        self.stdout.write(self.style.SUCCESS(f'Sent {sent} email(s) to {options["to"]}. Check the inbox and spam.'))