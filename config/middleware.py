from django.contrib.auth import get_user_model, login


class LocalAutoLogin:
    """No login page: the only authentication is the emailed Kronos OTP.

    Safe only because the server is bound to 127.0.0.1 and ALLOWED_HOSTS is localhost.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            user, _ = get_user_model().objects.get_or_create(
                username="local", defaults={"is_staff": True, "is_superuser": True})
            user.backend = "django.contrib.auth.backends.ModelBackend"
            login(request, user)
        return self.get_response(request)
