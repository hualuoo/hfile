from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


def find_user(username):
    """The account for what was typed in the login box: the exact username first, then the same
    whatever its case. Usernames are unique without case (User.Meta), so at most one matches:
    MySQL compares them that way anyway, SQLite only with iexact."""
    if not isinstance(username, str) or not username:
        return None
    User = get_user_model()
    users = User._default_manager
    user = users.filter(**{User.USERNAME_FIELD: username}).first()
    if user is None:
        user = users.filter(**{f"{User.USERNAME_FIELD}__iexact": username}).first()
    return user


class HFileBackend(ModelBackend):
    """Django's password login, with the username found whatever its case (find_user).
    Inactive users are refused, as by ModelBackend, also for sessions they already have."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if username is None or password is None:
            return None
        user = find_user(username)
        if user is None:
            # Hash anyway, so an unknown name takes as long as a wrong password (as ModelBackend does).
            get_user_model()().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def user_can_authenticate(self, user):
        """Active, and not the guest or the template (they have no usable password anyway)."""
        return super().user_can_authenticate(user) and not getattr(user, "is_builtin", False)
