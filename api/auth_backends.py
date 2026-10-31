from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailOrUsernameBackend(ModelBackend):
    """Lets people sign in with their email address as well as with their username.

    If the typed value looks like an email and exactly one active account uses it, the account's
    username is used. Anything else (no match, or two accounts sharing the email) falls through
    unchanged and simply fails like a wrong username.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username and '@' in username:
            matches = list(get_user_model().objects.filter(email__iexact=username.strip(), is_active=True)[:2])
            if len(matches) == 1:
                username = matches[0].get_username()
        return super().authenticate(request, username=username, password=password, **kwargs)