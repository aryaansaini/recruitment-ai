"""Token refresh that stops working once the user's password has changed.

SimpleJWT's CHECK_REVOKE_TOKEN stamps every token with a fingerprint of the password hash and rejects
access tokens whose fingerprint no longer matches. It does not look at the refresh endpoint, so an old
refresh token would still answer 200 there (the access token it hands out would then be rejected).
This view makes the refresh endpoint answer 401 as well, so a password reset really signs the account
out everywhere.
"""
from django.contrib.auth import get_user_model
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.utils import get_md5_hash_password
from rest_framework_simplejwt.views import TokenRefreshView


class RevocableTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        refresh = self.token_class(attrs['refresh'])      # raises TokenError (401) if invalid or expired
        user = get_user_model().objects.filter(
            **{api_settings.USER_ID_FIELD: refresh.payload.get(api_settings.USER_ID_CLAIM)}).first()
        if user is None or not api_settings.USER_AUTHENTICATION_RULE(user):
            raise AuthenticationFailed(self.error_messages['no_active_account'], 'no_active_account')
        if api_settings.CHECK_REVOKE_TOKEN and (
                refresh.payload.get(api_settings.REVOKE_TOKEN_CLAIM) != get_md5_hash_password(user.password)):
            raise AuthenticationFailed("The user's password has been changed.", 'password_changed')
        return super().validate(attrs)


class RevocableTokenRefreshView(TokenRefreshView):
    serializer_class = RevocableTokenRefreshSerializer
