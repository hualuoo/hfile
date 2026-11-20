"""SsoIconMiddleware: the origins of providers' http(s) icons (icons.py) added to img-src of the
Content-Security-Policy of HTML pages, which is strict otherwise (settings.SECURE_CSP). It comes right
after Django's CSP middleware in settings.MIDDLEWARE, so that it answers before that one writes the
header; a policy set already (siteconf's, or a view's own: response._csp_config) is extended, not
replaced, unless its img-src is 'none' or missing (then images were meant to be shut out). Without
http(s) icons it changes nothing."""

from django.conf import settings
from django.utils.csp import CSP

from . import icons


class SsoIconMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if settings.SETUP_MODE or not response.get("Content-Type", "").startswith("text/html"):
            return response
        if request.path.startswith(settings.STATIC_URL):  # the program's own files (a 404 among them)
            return response
        found = icons.origins()
        if not found:
            return response
        config = getattr(response, "_csp_config", None)
        if config is None:
            config = settings.SECURE_CSP
        if not config:  # a view that turned the policy off
            return response
        images = config.get("img-src")
        if isinstance(images, str):
            images = [images]
        if not isinstance(images, (list, tuple)) or CSP.NONE in images:
            return response
        # A copy (settings.SECURE_CSP is everybody's); values that aren't lists (a flag) as they are.
        config = {name: list(values) if isinstance(values, (list, tuple)) else values for name, values in config.items()}
        config["img-src"] = [*images, *(origin for origin in found if origin not in images)]
        response._csp_config = config
        return response
