"""The public pages and what the settings put on them (decision D33).

Public pages are the file browser (the home page, /<storage key>/...) and the share pages: what
visitors see. Only they show the announcement and the footer (备案号, Powered by HFile), and link the
admin's custom CSS and JS (/site/custom.css, /site/custom.js), with the CSP that the JS needs
(apps/siteconf/middleware.py). /manage/, the login, the installer and the Django admin never do, so a
broken script can't lock an admin out.

Passwords are never typed where the custom JS runs (D33: never in login), which may load scripts
from anywhere on https: a public page that links it leaves out the login dialog and the password
dialog: its 登录 goes to the login page (/login/?next=<the page>), and 修改密码, 两步验证 and 账号绑定 go
to the same page with ?auth=password, ?auth=totp or ?auth=sso, which links no custom code
(custom_code_allowed()) and opens the dialog (web/js/shell.js).

A view of a public page uses PublicPageMixin (a TemplateView), or puts public_context(request) in
its template's context and calls mark_public(); the file browser asks guest_landing() first, for the
匿名用户首页 setting.
"""

import re

from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.translation import gettext as _

from . import customjs
from .models import GuestHome, SiteSettings

FOREVER = "public, max-age=31536000, immutable"


def custom_code_allowed(request):
    """Whether this public page may link the custom CSS and JS: not with ?auth=login or ?auth=password,
    where a password is typed."""
    return "auth" not in request.GET


def public_context(request):
    """What base.html needs on a public page: public_page, custom_code (whether it links the custom
    CSS and JS), custom_js_linked (then no login or password dialog on it)."""
    allowed = custom_code_allowed(request)
    return {
        "public_page": True,
        "custom_code": allowed,
        "custom_js_linked": allowed and bool(SiteSettings.load().custom_js_version),
    }


def mark_public(response, request):
    """Marks a public page's response for the CSP (custom JS needs it relaxed)."""
    response.public_page = "page" if custom_code_allowed(request) else "login"
    return response


class PublicPageMixin:
    """For a TemplateView of a public page: base.html shows the announcement, the footer and the
    custom CSS and JS ({{ public_page }}, {{ custom_code }})."""

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), **public_context(self.request)}

    def render_to_response(self, context, **response_kwargs):
        return mark_public(super().render_to_response(context, **response_kwargs), self.request)


# ------------------------------------------------------------------ 匿名用户首页


LOGIN_PAGE = "/login/"  # accounts' login page while no secure login entry is set (apps/accounts/entry.py)


def browser_for_visitors() -> bool:
    """Whether visitors who aren't logged in get the file browser (匿名用户首页: 正常显示); else every page of
    it gives them guest_landing()'s page."""
    return SiteSettings.load().guest_home == GuestHome.NORMAL


def guest_landing(request):
    """What a visitor who isn't logged in gets instead of the file browser, or None (normal; or a
    logged-in user). The choices of 安全设置 → 匿名用户首页:

    - 跳转到登录页: the login page, /login/?next=<the address asked for>. With a secure login entry
      the only login page is /login/<entry>/, which visitors must not learn: the 401 page then, which
      says to log in and links nowhere.
    - 自定义页面: the admin's HTML as a page of its own, under the relaxed CSP. Asking to log in
      (/?auth=login&next=..., settings.LOGIN_URL) still leads to the login page, so admins can always
      log in.

    Only the browser's pages: shares, direct and short links work for visitors as before, and the
    API answers them by the guest user's grants (accounts, storages)."""
    if request.user.is_authenticated or browser_for_visitors():
        return None
    site = SiteSettings.load()
    asked_to_log_in = not custom_code_allowed(request)  # /?auth=login
    if site.guest_home == GuestHome.CUSTOM and not asked_to_log_in and site.guest_home_html.strip():
        return custom_page(request, site)
    if site.secure_login_entry:
        from apps.web.errors import error_page

        return error_page(request, 401, _("You need to log in to use this site."), login=False)
    target = request.GET.get("next", "/") if asked_to_log_in else request.get_full_path()
    if not (target.startswith("/") and url_has_allowed_host_and_scheme(target, allowed_hosts=None)) or any(
        ord(char) < 0x21 or char == "\\" for char in target
    ):
        target = "/"  # only a path of this site (the login page checks it again)
    return HttpResponseRedirect(f"{LOGIN_PAGE}?{urlencode({'next': target})}")


_DOCUMENT = re.compile(r"<!doctype|<html[\s>]", re.IGNORECASE)


def custom_page(request, site):
    """The admin's own page. A fragment (no <html> or doctype) gets a page around it: the site's
    name, icon and styles (so the MD3 classes work: <a class="btn btn-filled" href="/?auth=login">),
    and the custom CSS and JS."""
    html = site.guest_home_html
    if not _DOCUMENT.search(html[:2000]):
        html = render_to_string("siteconf/guest_page.html", {"site": site, "body": html}, request=request)
    response = HttpResponse(html)
    response.public_page = "custom"
    patch_vary_headers(response, ["Cookie"])  # logged-in users get the browser at the same address
    return response


# ------------------------------------------------------------------ /site/custom.css and /site/custom.js

_STYLE_TAGS = re.compile(r"</?style[^>]*>", re.IGNORECASE)
_PART = re.compile(r"[1-9][0-9]{0,5}")  # ASCII digits only: str.isdigit() also takes "²"


def _answer(request, text, content_type, version):
    if not text.strip():
        raise Http404(_("There is no custom content."))
    response = HttpResponse(text, content_type=f"{content_type}; charset=utf-8")
    # The pages link it with ?v=<fingerprint of the text>: that address never shows another text.
    if request.GET.get("v") == version:
        response["Cache-Control"] = FOREVER
    else:
        patch_cache_control(response, no_cache=True)
    return response


def custom_css(request):
    """The admin's custom CSS ("无须写 <style></style> 标签": they are taken out if written)."""
    site = SiteSettings.load()
    return _answer(request, _STYLE_TAGS.sub("", site.custom_css), "text/css", site.custom_css_version)


def custom_js(request):
    """The admin's custom JS: as written, or the loader of its scripts, or ?part=<n>, the n-th inline
    script (apps/siteconf/customjs.py)."""
    site = SiteSettings.load()
    version = site.custom_js_version
    part = request.GET.get("part")
    if part is None:
        def part_url(number):
            return f"{reverse('siteconf:custom-js')}?{urlencode({'part': number, 'v': version})}"

        return _answer(request, customjs.file_text(site.custom_js, part_url), "text/javascript", version)
    parts = customjs.inline_parts(site.custom_js) if site.custom_js.strip() else []
    if not _PART.fullmatch(part) or int(part) > len(parts):
        raise Http404(_("There is no such part of the custom JS."))
    return _answer(request, parts[int(part) - 1], "text/javascript", version)
