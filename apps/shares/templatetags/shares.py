"""{% share_storages as storages %}: the storages for the 分享列表 filter of /manage/ (every storage)."""

from django import template

from apps.storages.models import StorageSource

register = template.Library()


@register.simple_tag
def share_storages():
    return list(StorageSource.objects.order_by("position", "id").values("id", "name"))
