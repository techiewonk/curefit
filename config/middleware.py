from urllib.parse import urlsplit

from django.contrib.auth import get_user_model, login
from django.http import HttpResponseForbidden

LOCAL_HOSTS = {"localhost", "127.0.0.1"}
SAME_ORIGIN = {"same-origin", "none"}  # Sec-Fetch-Site: our own pages, or typed / bookmarked by the user


def from_another_site(request):
    """True when the browser says the request was started by another site (a link, <img> or form elsewhere).

    Modern browsers send Sec-Fetch-Site; older ones are judged by the Origin or Referer host."""
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None and site not in SAME_ORIGIN:
        return True
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if value and urlsplit(value).hostname not in LOCAL_HOSTS:
            return True  # includes "Origin: null" (sandboxed pages, file://)
    return False


class LocalAutoLogin:
    """No login page: the only authentication is the emailed Kronos OTP.

    Safe only because the server is bound to 127.0.0.1, ALLOWED_HOSTS is localhost, and requests that another
    website makes the browser send (which would be logged in automatically too) are refused here.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if from_another_site(request):
            return HttpResponseForbidden("Refused: this admin only answers its own pages. "
                                         "Open it by typing http://localhost:8000 in the address bar.")
        if not request.user.is_authenticated:
            user, _ = get_user_model().objects.get_or_create(
                username="local", defaults={"is_staff": True, "is_superuser": True})
            user.backend = "django.contrib.auth.backends.ModelBackend"
            login(request, user)
        return self.get_response(request)
