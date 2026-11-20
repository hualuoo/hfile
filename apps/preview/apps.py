from django.apps import AppConfig
from django.utils.translation import gettext_lazy

# The viewers' scripts, in order (preview.js first: the others use App.preview), and their style sheet.
SCRIPTS = tuple(f"preview/js/{name}.js" for name in
                ("preview", "image", "video", "audio", "text", "markdown", "pdfview", "kkfileview"))
STYLES = ("preview/css/preview.css",)


class PreviewConfig(AppConfig):
    """The file viewers: images, video (hls.js), audio, text and code (highlight.js and an editor),
    Markdown rendered on the server, PDF (pdf.js) and kkFileView. Every page that shows the file browser
    loads them (apps.files.pages.add_assets: the browse pages, and the share pages)."""

    name = "apps.preview"
    verbose_name = gettext_lazy("Preview")

    def ready(self):
        from apps.files import pages
        from apps.siteconf.middleware import public_policy

        from . import csp

        public_policy(csp.extend)
        pages.add_assets(scripts=SCRIPTS, styles=STYLES)
