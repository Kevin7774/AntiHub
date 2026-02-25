import html
import io
import re
import zipfile
from typing import Optional, Tuple

from config import RECOMMEND_MAX_TEXT_CHARS


def _clean_text(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text)
    return cleaned.strip()


def _decode_bytes(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        decoded = ""
        try:
            decoded = raw.decode(encoding, errors="ignore")
        except (LookupError, UnicodeDecodeError):
            decoded = ""
        if decoded:
            return decoded
    return raw.decode("utf-8", errors="ignore")


def _extract_docx(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml")
    except Exception:
        return ""
    text = _decode_bytes(xml)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return _clean_text(text)


def _extract_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                chunks.append("")
        return _clean_text(" ".join(chunks))
    except Exception:
        return ""


def extract_text_from_upload(filename: Optional[str], raw: bytes) -> Tuple[str, Optional[str]]:
    if not raw:
        return "", None
    name = (filename or "").lower().strip()
    warning = None
    if name.endswith(".docx"):
        text = _extract_docx(raw)
    elif name.endswith(".pdf"):
        text = _extract_pdf(raw)
        if not text:
            warning = "PDF 文本解析失败，已降级为基础匹配。"
    else:
        text = _decode_bytes(raw)
        text = _clean_text(text)
    if not text:
        return "", warning
    return text[:RECOMMEND_MAX_TEXT_CHARS], warning
