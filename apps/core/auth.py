from rest_framework.authentication import SessionAuthentication as DRFSessionAuthentication


class SessionAuthentication(DRFSessionAuthentication):
    """Cookie session auth that answers 401 (not 403) when nobody is logged in.

    DRF only uses 401 when the first authenticator names a WWW-Authenticate scheme.
    """

    def authenticate_header(self, request):
        return "Session"


def enforce_csrf(request):
    """Run Django's CSRF check even for anonymous callers.

    DRF skips CSRF for requests without a logged-in user, which would leave the
    login and register endpoints open to login-CSRF.
    """
    DRFSessionAuthentication().enforce_csrf(request)
