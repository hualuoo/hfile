from django.utils.functional import SimpleLazyObject

from .models import SiteSettings


def site_settings(request):
    """{{ site_settings.site_name }} in every template; read from the database only when used (the
    installer's pages, before there is a database, don't)."""
    return {"site_settings": SimpleLazyObject(SiteSettings.load)}
