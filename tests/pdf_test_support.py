"""Known-text PDF fixtures, independent of the competition corpus and annotations."""

from __future__ import annotations

import io
import re
from collections import Counter

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def pdf_bytes(pages: tuple[str, ...], *, title: str = "Synthetic report", password: str | None = None) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        if not text:
            continue
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
        })
        commands = ["BT /F1 10 Tf 40 750 Td 12 TL"]
        for index, line in enumerate(text.splitlines()):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if index:
                commands.append("T*")
            commands.append(f"({escaped}) Tj")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": title})
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner-password")  # noqa: S106
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def terms(text: str) -> Counter[str]:
    return Counter(re.findall(r"[a-z]+|\d+", text.lower()))


def text_metrics(actual: str, expected: str) -> tuple[float, float]:
    parsed, reference = terms(actual), terms(expected)
    overlap = sum((parsed & reference).values())
    recall = overlap / sum(reference.values()) if reference else float(not parsed)
    total = sum(parsed.values()) + sum(reference.values())
    f1 = 2 * overlap / total if total else 1.0
    return recall, f1
