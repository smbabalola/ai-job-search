from __future__ import annotations

import pytest

from webapp.services.docx_preview import docx_paragraphs
from tests.webapp.services.review_fixtures import docx_bytes


def test_a_docx_round_trips_to_paragraphs():
    paragraphs = docx_paragraphs(docx_bytes("Hello preview"))
    assert [p["text"] for p in paragraphs if p["text"]] == ["Hello preview"]


def test_non_docx_bytes_are_refused():
    with pytest.raises(ValueError):
        docx_paragraphs(b"%PDF-1.4 not a docx")


def test_documents_with_a_dtd_are_refused():
    import zipfile
    from io import BytesIO
    bomb = (b'<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>')
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        package.writestr("word/document.xml", bomb)
    with pytest.raises(ValueError):
        docx_paragraphs(stream.getvalue())
