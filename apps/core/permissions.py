"""Who may manage the site.

The site's admins are staff users (`is_staff`); the installer's first admin is a superuser too. The
/manage/ page and the manage/ API ask IsAdmin, so a change of who counts as an admin is made here.
"""

from rest_framework.permissions import BasePermission

from django.utils.translation import gettext_lazy


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active and user.is_staff)


class IsAdmin(BasePermission):
    message = gettext_lazy("Only administrators can do this.")

    def has_permission(self, request, view):
        return is_admin(request.user)
