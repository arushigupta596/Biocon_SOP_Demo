"""Validation for user-uploaded SOP documents.

Uploaded files are fully attacker-controlled: the filename reaches a subprocess
argument and a write path, and the contents reach the LLM prompt. Everything
here runs before a single byte is written to disk.
"""
from __future__ import annotations

import io
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from src.guardrails.limits import (
    MAX_DOCX_PARAGRAPHS,
    MAX_PDF_PAGES,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    MAX_ZIP_ENTRIES,
    MAX_ZIP_UNCOMPRESSED_BYTES,
)


class GuardrailError(Exception):
    """A containment invariant was violated. Never shown raw to the user."""


@dataclass
class Violation:
    rule: str
    message: str           # plain language, shown to the user
    action: str = "rejected"   # rejected | truncated | throttled | flagged
    detail: str = ""


@dataclass
class UploadDecision:
    ok: bool
    safe_name: str = ""
    sniffed_type: str | None = None
    unit_count: int = 0          # PDF pages or DOCX paragraphs
    violations: list[Violation] = field(default_factory=list)
    renamed: bool = False

    @property
    def blocking(self) -> list[Violation]:
        return [v for v in self.violations if v.action == "rejected"]


# Unicode bidi controls: a name can render as ".pdf" while ending in something else.
_BIDI = dict.fromkeys(
    list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))
)
_CONTROL = dict.fromkeys(list(range(0x00, 0x20)) + [0x7F])

_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def safe_filename(name: str) -> str:
    """Reduce an arbitrary upload name to a safe, contained basename.

    Path("sops") / "../corpus/x.pdf" escapes the directory, and an absolute
    right-hand operand discards the left entirely, so the basename must be
    taken before the name is ever joined to a directory.
    """
    # Cut any directory component, in both separator conventions.
    name = name.replace("\\", "/").split("/")[-1]
    name = Path(name).name

    name = name.translate(_CONTROL).translate(_BIDI)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch if ch in _ALLOWED else "_" for ch in name)

    while "__" in name:
        name = name.replace("__", "_")
    while ".." in name:
        name = name.replace("..", ".")

    # A leading dash would be consumed by argparse as a flag, not a path.
    name = name.lstrip("-.")

    if not name:
        return "upload"

    p = Path(name)
    suffix = p.suffix[:16]
    stem = p.stem[:120] or "upload"
    return f"{stem}{suffix}"


def assert_contained(path: Path, root: Path) -> Path:
    """Guarantee `path` resolves inside `root`, or raise."""
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise GuardrailError(f"path escapes its root: {resolved} not under {root_resolved}")
    return resolved


def sniff_type(data: bytes) -> str | None:
    """Identify the real container type from magic bytes, not the extension."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if len(names) > MAX_ZIP_ENTRIES:
                    return None
                total = sum(zi.file_size for zi in zf.infolist())
                if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                    return None
                # Distinguishes a real .docx from .xlsx/.pptx/a renamed zip.
                if "word/document.xml" in names:
                    return "docx"
        except Exception:
            return None
    return None


def trial_parse(data: bytes, kind: str) -> tuple[bool, int, str]:
    """Open the document in memory. Returns (ok, unit_count, error)."""
    try:
        if kind == "pdf":
            import fitz
            with fitz.open(stream=data, filetype="pdf") as doc:
                if doc.needs_pass:
                    return False, 0, "document is password-protected"
                return True, doc.page_count, ""
        if kind == "docx":
            import docx
            document = docx.Document(io.BytesIO(data))
            return True, len(document.paragraphs), ""
        return False, 0, f"unsupported type {kind!r}"
    except Exception as exc:
        return False, 0, f"{type(exc).__name__}: {exc}"


def validate_upload(original_name: str, data: bytes) -> UploadDecision:
    """Run every pre-write check, cheapest first."""
    violations: list[Violation] = []

    # 1. Size
    if len(data) > MAX_UPLOAD_BYTES:
        size_mb = len(data) / (1024 * 1024)
        return UploadDecision(
            ok=False,
            violations=[Violation(
                "oversize",
                f"That file is {size_mb:.1f} MB. The scanner accepts SOPs up to "
                f"{MAX_UPLOAD_MB} MB. Nothing was uploaded.",
                detail=f"{len(data)} bytes",
            )],
        )

    if not data:
        return UploadDecision(
            ok=False,
            violations=[Violation("empty", "That file is empty. Nothing was uploaded.")],
        )

    # 2. Filename
    safe_name = safe_filename(original_name)
    renamed = safe_name != original_name

    # 3. Real container type
    sniffed = sniff_type(data)
    if sniffed is None:
        return UploadDecision(
            ok=False, safe_name=safe_name, renamed=renamed,
            violations=[Violation(
                "unknown_type",
                "This does not look like a Word or PDF document. Nothing was uploaded.",
            )],
        )

    # 4. Extension must agree with contents
    declared = Path(safe_name).suffix.lower().lstrip(".")
    if declared != sniffed:
        return UploadDecision(
            ok=False, safe_name=safe_name, renamed=renamed, sniffed_type=sniffed,
            violations=[Violation(
                "type_mismatch",
                f"This file is named .{declared or '(none)'} but its contents are a "
                f"{sniffed.upper()}. Rename it with the correct extension, or "
                f"re-export it.",
                detail=f"declared={declared!r} sniffed={sniffed!r}",
            )],
        )

    # 5. Trial parse
    ok, unit_count, error = trial_parse(data, sniffed)
    if not ok:
        return UploadDecision(
            ok=False, safe_name=safe_name, renamed=renamed, sniffed_type=sniffed,
            violations=[Violation(
                "unparseable",
                f"This document could not be opened ({error}). If it is "
                f"password-protected, remove the protection and re-upload.",
                detail=error,
            )],
        )

    # 6. Page / paragraph cap
    cap = MAX_PDF_PAGES if sniffed == "pdf" else MAX_DOCX_PARAGRAPHS
    unit = "pages" if sniffed == "pdf" else "paragraphs"
    if unit_count > cap:
        return UploadDecision(
            ok=False, safe_name=safe_name, renamed=renamed,
            sniffed_type=sniffed, unit_count=unit_count,
            violations=[Violation(
                "too_large",
                f"This document has {unit_count} {unit}. The scanner accepts up to "
                f"{cap}. Nothing was uploaded.",
            )],
        )

    if renamed:
        violations.append(Violation(
            "renamed",
            f"Saved as {safe_name} (the uploaded name contained characters that "
            f"were removed).",
            action="flagged",
        ))

    return UploadDecision(
        ok=True, safe_name=safe_name, renamed=renamed,
        sniffed_type=sniffed, unit_count=unit_count, violations=violations,
    )
