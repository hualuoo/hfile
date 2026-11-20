from django.contrib import admin
from django.utils.translation import gettext_lazy

from .models import UpgradeLog


@admin.register(UpgradeLog)
class UpgradeLogAdmin(admin.ModelAdmin):
    """The upgrade log. The program writes it as it starts; here it can only be read."""

    list_display = ["finished_at", "previous", "version", "migration_count", "backup"]
    fields = ["version", "previous", "migrations", "backup", "started_at", "finished_at"]

    @admin.display(description=gettext_lazy("Migrations"))
    def migration_count(self, obj):
        return len(obj.migrations)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
