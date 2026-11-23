"""The icons a storage source may have (StorageSource.icon), shown in the navigation rail, the file
browser's storage menu and the list at "/". Each is a Material Symbols name in the site's sprite with its
"-fill" form, the rail's current item (scripts/icons.py lists them in ICONS and FILLED; apps/web/tests.py
checks the sprite has them). The labels are for the admin's picker (screen readers, the tooltip).
"""

from django.utils.translation import pgettext_lazy

DEFAULT = "storage"

ICONS = {
    "storage": pgettext_lazy("storage icon", "Storage"),
    "cloud": pgettext_lazy("storage icon", "Cloud"),
    "hard_drive": pgettext_lazy("storage icon", "Hard drive"),
    "dns": pgettext_lazy("storage icon", "Server"),
    "database": pgettext_lazy("storage icon", "Database"),
    "folder": pgettext_lazy("storage icon", "Folder"),
    "folder_shared": pgettext_lazy("storage icon", "Shared folder"),
    "backup": pgettext_lazy("storage icon", "Backup"),
    "archive": pgettext_lazy("storage icon", "Archive"),
    "photo_library": pgettext_lazy("storage icon", "Photos"),
    "photo_camera": pgettext_lazy("storage icon", "Camera"),
    "video_library": pgettext_lazy("storage icon", "Videos"),
    "movie": pgettext_lazy("storage icon", "Movies"),
    "library_music": pgettext_lazy("storage icon", "Music"),
    "description": pgettext_lazy("storage icon", "Documents"),
    "menu_book": pgettext_lazy("storage icon", "Books"),
    "school": pgettext_lazy("storage icon", "Study"),
    "work": pgettext_lazy("storage icon", "Work"),
    "code": pgettext_lazy("storage icon", "Code"),
    "terminal": pgettext_lazy("storage icon", "Terminal"),
    "science": pgettext_lazy("storage icon", "Science"),
    "sports_esports": pgettext_lazy("storage icon", "Games"),
    "download": pgettext_lazy("storage icon", "Downloads"),
    "share": pgettext_lazy("storage icon", "Sharing"),
    "public": pgettext_lazy("storage icon", "Public"),
    "lock": pgettext_lazy("storage icon", "Private"),
    "star": pgettext_lazy("storage icon", "Star"),
    "favorite": pgettext_lazy("storage icon", "Favorite"),
    "bookmark": pgettext_lazy("storage icon", "Bookmark"),
    "rocket_launch": pgettext_lazy("storage icon", "Rocket"),
    "pets": pgettext_lazy("storage icon", "Pets"),
    "apartment": pgettext_lazy("storage icon", "Company"),
    "family_restroom": pgettext_lazy("storage icon", "Family"),
}


def shown(name: str) -> str:
    """The icon to show for a stored name: itself, or the default for one that isn't offered (any more)."""
    return name if name in ICONS else DEFAULT
