"""Error pages, drawn in place: at the address that failed, with its status, never by redirecting to
an error address with the message in it (decision D38). The API (/api/...) gets the same status with
JSON {"detail"} instead.

    return error_page(request, 404, "这个直链已经失效了。")
    return error_page(request, 429, retry_after=30)

Templates: apps/web/templates/<status>.html on error_base.html, standalone (no page frame and no database
needed: a 500 may be the frame's own). Django's own errors come here too (config/urls.py handler400
and the others, settings.CSRF_FAILURE_VIEW).
"""

import math
import re

from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy, ngettext

from apps.core.exceptions import csrf_message

# What each page says when the caller gives no message, for the API's JSON as well.
DEFAULT_MESSAGES = {
    400: gettext_lazy("The site can't handle this request: the address or what was sent is wrong."),
    401: gettext_lazy("Please log in first."),
    403: gettext_lazy("You don't have permission to do this."),
    404: gettext_lazy("Not found."),
    413: gettext_lazy("What was sent is too large."),
    429: gettext_lazy("Too many requests. Please try again later."),
    500: gettext_lazy("The server ran into an error. Please try again later."),
    502: gettext_lazy("Another server the site asked returned an error. Please try again later."),
}


def wants_json(request):
    return request.path.startswith("/api/")


def error_page(request, status, message="", *, retry_after=None, login=None, code=None, heading="", actions=None,
               home=True):
    """The error page for `status` (400, 401, 403, 404, 413, 429, 500 or 502), or JSON for the API.

    - message: what went wrong, in Chinese; written as text. Leave it out for the page's own words.
      Never put what the request sent into it unchecked.
    - retry_after (429): seconds, for the Retry-After header and the message.
    - login (401): whether the page offers to log in; by default when the site shows a login
      button (SiteSettings.login_visible).
    - code: a stable name for the API's JSON ("code"), where the page's script branches on it.
    - heading: the page's title instead of its own, when the status alone says too little
      ("这个 GitHub 账号还没有绑定用户").
    - actions: [(label, path), ...], the ways on instead of 回到首页 (the first one stands out): single
      sign-on's 重新登录, say. Paths of this site only.
    - home: False leaves 回到首页 out (with no actions the page has no button): the access control's 403,
      whose rule refuses the home page too.
    """
    if status == 429 and retry_after is not None and not message:
        message = ngettext("Too many requests. Please try again in %(n)s second.", "Too many requests. Please try again in %(n)s seconds.", math.ceil(retry_after)) % {"n": math.ceil(retry_after)}
    if wants_json(request):
        data = {"detail": message or DEFAULT_MESSAGES.get(status, DEFAULT_MESSAGES[400])}
        if code:
            data["code"] = code
        response = JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
    else:
        context = {"message": message, "status": status, "heading": heading, "actions": actions or [], "home": home}
        if status == 401:
            context["login"] = _login_visible() if login is None else login
            context["next"] = request.get_full_path()
            context["login_page"] = "/login/"  # accounts' login page (with no secure login entry set)
        response = HttpResponse(render_to_string(f"{status}.html", context, request=request), status=status)
    if retry_after is not None:
        response["Retry-After"] = str(math.ceil(retry_after))
    return response


def _login_visible():
    from apps.siteconf.models import SiteSettings

    try:
        return SiteSettings.load().login_visible
    except Exception:  # the page shouldn't fail for it; no button then
        return False


# ------------------------------------------------------------------ Django's handlers (config/urls.py)


def bad_request(request, exception=None):
    return error_page(request, 400)


def permission_denied(request, exception=None):
    return error_page(request, 403, _own_message(exception))


def page_not_found(request, exception=None):
    return error_page(request, 404, _own_message(exception))


def server_error(request):
    return error_page(request, 500)


def csrf_failure(request, reason=""):
    """settings.CSRF_FAILURE_VIEW: a form of the Django admin sent without its token, say."""
    return error_page(request, 403, csrf_message(reason), code="csrf_failed")


def _own_message(exception):
    """The message of a PermissionDenied or Http404 the site raised itself, in Chinese; Django's
    own (English, or a dict with the address in it) are left out."""
    args = getattr(exception, "args", ())
    if len(args) == 1 and isinstance(args[0], str) and _CJK.search(args[0]):
        return args[0]
    return ""


_CJK = re.compile(r"[\u4e00-\u9fff]")
