"""Fictional documents for the hospital-response demonstration; no real patient data."""

import base64
import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

MESSAGE = (
    "We have attached 40 pages of records. However, the surgical notes are unavailable. "
    "Please provide an updated authorization."
)


def pdf_attachment(kind="general records", pages=40):
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = DecodedStreamObject()
        content.set_data(
            (
                f"BT /F1 12 Tf 72 720 Td (Record type: {kind}) Tj "
                f"0 -20 Td (Fictional document page {index + 1}) Tj ET"
            ).encode()
        )
        page[NameObject("/Contents")] = writer._add_object(content)
    target = io.BytesIO()
    writer.write(target)
    return {
        "filename": "hospital-records.pdf",
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(target.getvalue()).decode(),
    }


def attachment(text):
    return {
        "filename": "authorization.txt",
        "content_type": "text/plain",
        "content_base64": base64.b64encode(text.encode()).decode(),
    }
