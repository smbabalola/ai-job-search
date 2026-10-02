"""The CV template registry (Bundle 7 spec §14.5). Adding a template is a data
change. Templates apply only to AI_GENERATED / AI_TAILORED versions; uploaded
CVs are always used byte-exact."""
from __future__ import annotations

CV_TEMPLATES: dict[str, dict] = {
    "standard@1": {"renderer": "cv_document_renderer", "formats": ("docx",)},
}
