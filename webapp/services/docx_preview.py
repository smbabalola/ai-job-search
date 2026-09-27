"""Read-only DOCX preview (6D-A spec §7.1): paragraphs and their style names
from the exact stored bytes. For human inspection only; nothing that is sent
is ever derived from a preview."""
from __future__ import annotations

import zipfile
from io import BytesIO
from xml.etree import ElementTree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_paragraphs(data: bytes) -> list[dict[str, str]]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as package:
            xml = package.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("not a DOCX document") from exc
    # WordprocessingML never needs a DTD; refusing any DOCTYPE/ENTITY rules out
    # entity-expansion and external-entity attacks before the stdlib parser runs.
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ValueError("DOCX document XML must not declare a DTD")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ValueError("DOCX document XML is malformed") from exc
    out = []
    for paragraph in root.iter(f"{_W}p"):
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        text = "".join(t.text or "" for t in paragraph.iter(f"{_W}t"))
        out.append({"style": style.get(f"{_W}val") if style is not None else "", "text": text})
    return out
