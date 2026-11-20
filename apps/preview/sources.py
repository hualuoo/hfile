"""What the preview app takes from the files app (RUN/files/readme_notes.md): the signed /pd/ links the
viewers hand to the preview endpoints (W2) and the kinds of files (W3).

The viewers get a file's signed same-origin link from ctx.inlineUrl(item) (the files API's inline_url,
W1 same_origin=True, inline=True) and send it as the proof that the visitor may read the file.
"""

from __future__ import annotations

from django.utils.translation import gettext as _

from apps.files import kinds, signing
from apps.storages.access import AccessDenied


def verify(url: str) -> signing.Link:
    """The file a viewer's signed link stands for: a /pd/ path of this site with its query (an absolute
    URL isn't taken: nothing tells it is this site's). Parameters the link doesn't have are ignored, such
    as the fullfilename kkFileView appends. Raises AccessDenied link_invalid or link_expired; the caller
    then asks apps.storages.access.check_served()."""
    if not isinstance(url, str) or not url.startswith(signing.PREFIX) or len(url) > 10_000:
        raise AccessDenied(signing.INVALID, _("Invalid download link."))
    try:
        return signing.verify_url(url)
    except signing.BadLink as error:
        raise AccessDenied(error.code, error.message) from None


def kind_of(name: str, site) -> str:
    """apps.files.kinds.kind_of: image, video, audio, text, markdown, pdf, ... by the site's suffix lists."""
    return kinds.kind_of(name, site)
