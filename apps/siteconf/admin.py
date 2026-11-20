from django.contrib import admin

from .models import SiteSettings
from .serializers import GROUPS


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Only to look at, by page: they are changed on /manage/, whose checks (apps/siteconf/serializers.py) a
    form here would skip (superuser-only HTML and JS, the encrypted OnlyOffice secret, the reserved
    names). The secret isn't shown at all."""

    list_display = ["site_name", "updated_at"]
    fieldsets = [
        (serializer.label, {"fields": [name for name in serializer.Meta.fields
                                       if name in {field.name for field in SiteSettings._meta.fields} and name != "onlyoffice_secret"]})
        for serializer in GROUPS.values()
    ]

    def has_add_permission(self, request):
        return False  # one row, made by SiteSettings.load()

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
