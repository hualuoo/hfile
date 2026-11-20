"""The 新增 menu of /manage/#sso: a provider's form filled in for a well-known service (checked against
each one's documentation and endpoints in November 2026). Data only; the admin still enters the Client
ID and Secret of the app registered there, with the callback address the form shows.

Every preset binds on the stable id (GitHub's and Gitee's numeric `id`, OpenID Connect's `sub`), never
on a name or an email address, and proposes the username and nickname a new user gets.

Icons are data: URIs, shown in an <img>: the marks of GitHub, Gitee and GitLab are Simple Icons 16.36.0
(CC0 1.0, https://simpleicons.org; the brand colors are theirs too), the two others are drawn here
(neutral: a letter in a rounded square, and Material Symbols' key, Apache 2.0).
"""

import base64

from django.utils.translation import gettext_lazy

_SIMPLE_ICONS = {
    # simple-icons@16.36.0/icons/github.svg, gitee.svg, gitlab.svg: the path, and the brand color.
    "github": (
        "#181717",
        "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338"
        ".724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1."
        "236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3."
        "22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3."
        "285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096."
        "81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12",
    ),
    "gitee": (
        "#C71D23",
        "M11.984 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.016 0zm6.09 5.333c.328 0 .5"
        "93.266.592.593v1.482a.594.594 0 0 1-.593.592H9.777c-.982 0-1.778.796-1.778 1.778v5.63c0 .327.266.592.593.592h5.63c.982 0 "
        "1.778-.796 1.778-1.778v-.296a.593.593 0 0 0-.592-.593h-4.15a.592.592 0 0 1-.592-.592v-1.482a.593.593 0 0 1 .593-.592h6.8"
        "15c.327 0 .593.265.593.592v3.408a4 4 0 0 1-4 4H5.926a.593.593 0 0 1-.593-.593V9.778a4.444 4.444 0 0 1 4.445-4.444h8.296Z",
    ),
    "gitlab": (
        "#FC6D26",
        "m23.6004 9.5927-.0337-.0862L20.3.9814a.851.851 0 0 0-.3362-.405.8748.8748 0 0 0-.9997.0539.8748.8748 0 0 0-.29.4399l-2.2"
        "055 6.748H7.5375l-2.2057-6.748a.8573.8573 0 0 0-.29-.4412.8748.8748 0 0 0-.9997-.0537.8585.8585 0 0 0-.3362.4049L.4332 9."
        "5015l-.0325.0862a6.0657 6.0657 0 0 0 2.0119 7.0105l.0113.0087.03.0213 4.976 3.7264 2.462 1.8633 1.4995 1.1321a1.0085 1.0"
        "085 0 0 0 1.2197 0l1.4995-1.1321 2.4619-1.8633 5.006-3.7489.0125-.01a6.0682 6.0682 0 0 0 2.0094-7.003z",
    ),
}

# Material Symbols Rounded "key" (as in apps/web/static/web/icons.svg).
_KEY_PATH = (
    "M280-400q-33 0-56.5-23.5T200-480q0-33 23.5-56.5T280-560q33 0 56.5 23.5T360-480q0 33-23.5 56.5T280-400Zm0 160q-100 "
    "0-170-70T40-480q0-100 70-170t170-70q67 0 121.5 33t86.5 87h335q8 0 15.5 3t13.5 9l80 80q6 6 8.5 13t2.5 15q0 8-2.5 15t-8.5 "
    "13L805-325q-5 5-12 8t-14 4q-7 1-14-1t-13-7l-52-39-57 43q-5 4-11 6t-12 2q-6 0-12.5-2t-11.5-6l-61-43h-47q-32 54-86.5 87T280"
    "-240Zm0-80q56 0 98.5-34t56.5-86h125l58 41v.5-.5l82-61 71 55 75-75h-.5.5l-40-40v-.5.5H435q-14-52-56.5-86T280-640q-66 0-113 "
    "47t-47 113q0 66 47 113t113 47Z"
)


def _data_uri(svg):
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _simple_icon(name):
    color, path = _SIMPLE_ICONS[name]
    return _data_uri(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="{color}" d="{path}"/></svg>')


ICONS = {
    **{name: _simple_icon(name) for name in _SIMPLE_ICONS},
    # Not Logto's logo: an "L" in a rounded square.
    "logto": _data_uri(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#5B4CE0"/>'
        '<path d="M8.5 6.5v11h7.5" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "oidc": _data_uri(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960"><path fill="#5F6368" d="{_KEY_PATH}"/></svg>'
    ),
}

PRESETS = [
    {
        "key": "github",
        "label": "GitHub",
        "help": gettext_lazy("Create an app in GitHub's Settings → Developer settings → OAuth Apps, "
                "with the callback URL below as its Authorization callback URL. Scope "
                "isn't needed: reading the public profile (ID, username, name) is enough."),
        "values": {
            "code": "github", "name": "GitHub", "icon": ICONS["github"],
            "auth_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "userinfo_url": "https://api.github.com/user",
            "scope": "", "token_auth": "post",
            "id_field": "id", "username_field": "login", "nickname_field": "name",
        },
    },
    {
        "key": "gitee",
        "label": "Gitee",
        "help": gettext_lazy("Create an app in Gitee's Settings → Third-party apps, with the callback "
                             "URL below as its callback address, and check the user_info permission."),
        "values": {
            "code": "gitee", "name": "Gitee", "icon": ICONS["gitee"],
            "auth_url": "https://gitee.com/oauth/authorize",
            "token_url": "https://gitee.com/oauth/token",
            "userinfo_url": "https://gitee.com/api/v5/user",
            "scope": "user_info", "token_auth": "post",
            "id_field": "id", "username_field": "login", "nickname_field": "name",
        },
    },
    {
        "key": "gitlab",
        "label": "GitLab",
        "help": gettext_lazy("Create an application in GitLab's Preferences → Applications (check "
                "Confidential, choose the openid and profile scopes), with the callback "
                "URL below as its Redirect URI. For a GitLab of your own, replace "
                "gitlab.com with its address, or fill in from its .well-known address."),
        "values": {
            "code": "gitlab", "name": "GitLab", "icon": ICONS["gitlab"],
            "auth_url": "https://gitlab.com/oauth/authorize",
            "token_url": "https://gitlab.com/oauth/token",
            "userinfo_url": "https://gitlab.com/oauth/userinfo",
            "scope": "openid profile", "token_auth": "post",
            "id_field": "sub", "username_field": "preferred_username", "nickname_field": "name",
        },
    },
    {
        "key": "logto",
        "label": "Logto",
        "help": gettext_lazy('Create a "Traditional web" application in the Logto console, with the '
                "callback URL below as its Redirect URI. Replace your-tenant in the three "
                "endpoints with your tenant, or fill in from the .well-known address "
                "(https://your-tenant.logto.app/oidc/.well-known/openid-configuration). Logto's "
                "traditional web applications only take the Client Secret over HTTP Basic."),
        "values": {
            "code": "logto", "name": "Logto", "icon": ICONS["logto"],
            "auth_url": "https://your-tenant.logto.app/oidc/auth",
            "token_url": "https://your-tenant.logto.app/oidc/token",
            "userinfo_url": "https://your-tenant.logto.app/oidc/me",
            "scope": "openid profile", "token_auth": "basic",
            "id_field": "sub", "username_field": "username", "nickname_field": "name",
        },
    },
    {
        "key": "oidc",
        "label": gettext_lazy("Generic OpenID Connect"),
        "help": gettext_lazy('OpenID Connect services such as Keycloak, Authentik, Authelia and Casdoor: click "Fill '
                'in from a .well-known address" first and enter its OpenID configuration address (usually '
                "issuer-address/.well-known/openid-configuration), then fill in the Client ID and Secret."),
        "values": {
            "code": "oidc", "name": "OpenID Connect", "icon": ICONS["oidc"],
            "auth_url": "", "token_url": "", "userinfo_url": "",
            "scope": "openid profile", "token_auth": "basic",
            "id_field": "sub", "username_field": "preferred_username", "nickname_field": "name",
        },
    },
]
