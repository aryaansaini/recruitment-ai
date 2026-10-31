import json
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

BREVO_URL = 'https://api.brevo.com/v3/smtp/email'


class BrevoEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        key = getattr(settings, 'BREVO_API_KEY', '')
        if not key:
            if self.fail_silently:
                return 0
            raise ValueError('BREVO_API_KEY is not set.')

        sent = 0
        for message in email_messages:
            name, address = parseaddr(message.from_email or settings.DEFAULT_FROM_EMAIL)
            sender = {'email': address}
            if name:
                sender['name'] = name
            payload = {
                'sender': sender,
                'to': [{'email': r} for r in message.to],
                'subject': message.subject,
                'textContent': message.body,
            }
            request = urllib.request.Request(
                BREVO_URL,
                data=json.dumps(payload).encode('utf-8'),
                headers={'api-key': key, 'Content-Type': 'application/json', 'accept': 'application/json'},
                method='POST',
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    if 200 <= response.status < 300:
                        sent += 1
            except urllib.error.HTTPError as exc:
                if not self.fail_silently:
                    detail = exc.read().decode('utf-8', 'replace')[:300]
                    raise RuntimeError(f'Brevo rejected the email ({exc.code}): {detail}') from exc
            except Exception:
                if not self.fail_silently:
                    raise
        return sent