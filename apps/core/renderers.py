from rest_framework import renderers


class JSONRenderer(renderers.JSONRenderer):
    """DRF's, except that text with half a UTF-16 pair in it ("\\ud800", which UTF-8 can't encode)
    comes out escaped instead of failing the whole answer with a 500. Nothing stores such text (DRF's
    CharField and apps/core/parsers.py refuse it), but an error message may quote what was sent."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        try:
            return super().render(data, accepted_media_type, renderer_context)
        except UnicodeEncodeError:
            return _AsciiJSONRenderer().render(data, accepted_media_type, renderer_context)


class _AsciiJSONRenderer(renderers.JSONRenderer):
    ensure_ascii = True  # every other character as a \\uXXXX escape: plain ASCII, always encodable
