"""Content-integrity guardrails for the gap detector.

Four concerns, all pure functions with no I/O and no API calls:

1. sanitise_untrusted  — neutralise prompt injection in uploaded SOP text
2. extract_json_array  — recover a JSON array from an imperfect model reply
3. coerce_item         — repair near-miss items before schema validation
4. verify_finding      — prove a citation came from the retrieved corpus

verify_finding is the load-bearing one. Without it the model can invent a
regulatory citation and it lands in a client-facing audit report.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from src.schemas import (
    GUARDRAILS_VERSION,
    GapResult,
    VerificationFlag,
    VerificationStatus,
)

__all__ = [
    "GUARDRAILS_VERSION", "CONFIDENCE_THRESHOLD", "EXCERPT_CONTAINMENT_THRESHOLD",
    "Verification", "sanitise_untrusted", "extract_json_array", "coerce_item",
    "verify_finding", "reg_keys",
]

# Mirrors CONFIDENCE_THRESHOLD in tests/test_demo_gaps.py.
CONFIDENCE_THRESHOLD = 0.80
# Fraction of the excerpt's content tokens that must appear in a single
# retrieved chunk. A genuine verbatim quote reflowed by the model still clears
# this; a fabricated sentence essentially never does, because it would need
# three quarters of its content words to co-occur in one 512-token chunk.
EXCERPT_CONTAINMENT_THRESHOLD = 0.75
MIN_EXCERPT_TOKENS = 4


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "the a an of to and or in for be is are was were shall must may should "
    "this that these those with by on at as it its from not no any all such "
    "which when where if then than each other into".split()
)

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _norm(s: str) -> str:
    """Lowercase, strip accents, reduce every non-alphanumeric run to a space.

    Makes hyphenation, ligatures and PDF line-break artifacts irrelevant.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", s.lower()).strip()


def _tokens(s: str) -> set[str]:
    return {
        t for t in _norm(s).split()
        if t not in _STOPWORDS and not (len(t) == 1 and t.isdigit())
    }


def _norm_section(s: str) -> str:
    """'§7.3', 'Section 7.3', '7.3.' all collapse to '7.3'."""
    s = (s or "").replace("§", " ")
    s = re.sub(r"(?i)\bsections?\b", " ", s)
    s = re.sub(r"(?i)\bclause\b", " ", s)
    return re.sub(r"[^0-9.]+", "", s).strip(".")


# ---------------------------------------------------------------------------
# 1. Untrusted input sanitisation
# ---------------------------------------------------------------------------

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
# Our own delimiter shape, e.g. "--- REGULATORY CONTEXT (retrieved from corpus) ---"
_DELIMITER_LINE = re.compile(r"^[ \t]*-{2,}[ \t]*[A-Z][A-Z \t()/,'-]{3,}[ \t]*-{2,}[ \t]*$", re.M)
_SOURCE_MARKER = re.compile(r"\[\s*SOURCE\s*\d+\s*\]", re.I)
_OUR_TAGS = re.compile(
    r"</?\s*(untrusted_sop_clause|regulatory_context|sop_metadata)\s*[^>]*>", re.I
)
_ROLE_MARKER = re.compile(r"^[ \t]*(system|assistant|user|human)[ \t]*:", re.I | re.M)
_FENCE = re.compile(r"^[ \t]*`{3,}.*$", re.M)
_CONTRACT_PHRASES = re.compile(
    r"(?i)(OUTPUT\s+CONTRACT|SEVERITY\s+DEFINITIONS|TRUST\s+BOUNDARY"
    r"|SOP\s+CLAUSE\s+TEXT|REGULATORY\s+CONTEXT"
    r"|ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"
    r"|disregard\s+(?:all\s+)?(?:previous|prior|above))"
)
_MANY_NEWLINES = re.compile(r"\n{3,}")


def sanitise_untrusted(text: str) -> tuple[str, int]:
    """Neutralise prompt-injection payloads in document text.

    Returns (cleaned, hits). Any hit > 0 means the document contained something
    shaped like our own prompt scaffolding, which is itself worth flagging.
    """
    if not text:
        return "", 0

    hits = 0
    out = text

    cleaned = _ILLEGAL_XML.sub("", out)
    if cleaned != out:
        hits += 1
    out = cleaned

    for pattern, replacement in (
        (_DELIMITER_LINE, "[delimiter removed]"),
        (_SOURCE_MARKER, "[marker removed]"),
        (_OUR_TAGS, "[tag removed]"),
        (_FENCE, "[fence removed]"),
        (_CONTRACT_PHRASES, "[redacted]"),
    ):
        out, n = pattern.subn(replacement, out)
        hits += n

    out, n = _ROLE_MARKER.subn("", out)
    hits += n

    out = _MANY_NEWLINES.sub("\n\n", out)
    return out, hits


# ---------------------------------------------------------------------------
# 2. JSON extraction
# ---------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys([0xFEFF, 0x200B, 0x200C, 0x200D, 0x2060])
# Deliberately searched ANYWHERE, not only at position 0: "Here is the JSON:\n```json…"
_FENCED = re.compile(r"`{3,}\s*(?:json|JSON)?\s*\n?(.*?)`{3,}", re.S)


def _balanced_array(s: str) -> Optional[str]:
    """Scan from the first '[' to its matching ']', respecting string literals."""
    start = s.find("[")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def extract_json_array(raw: str) -> tuple[Optional[list], str]:
    """Recover a list of finding dicts from a model reply.

    Returns (items, how). `items` is None when nothing parseable was found;
    `how` labels the route taken, for logging.
    """
    if raw is None:
        return None, "no content"

    text = str(raw).translate(_ZERO_WIDTH).strip()
    if not text:
        return None, "empty"

    candidates: list[tuple[str, str]] = []
    m = _FENCED.search(text)
    if m:
        candidates.append((m.group(1).strip(), "fenced"))
    candidates.append((text, "raw"))
    balanced = _balanced_array(text)
    if balanced:
        candidates.append((balanced, "bracket-scan"))

    for candidate, how in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue

        if isinstance(data, list):
            return data, how
        if isinstance(data, dict):
            # {"gaps": [...]} — single list-valued key
            lists = [v for v in data.values() if isinstance(v, list)]
            if len(lists) == 1:
                return lists[0], f"{how}/unwrapped"
            # A bare finding object rather than an array
            if "gap_description" in data or "sop_clause" in data:
                return [data], f"{how}/wrapped"
    return None, "unparseable"


# ---------------------------------------------------------------------------
# 3. Item coercion
# ---------------------------------------------------------------------------

def coerce_item(item: dict) -> tuple[dict, list[str]]:
    """Repair near-miss items so a real finding is not lost to a formatting slip.

    'Critical' instead of 'CRITICAL' currently raises ValidationError and the
    finding is silently dropped. This recovers it.
    """
    flags: list[str] = []
    out = dict(item)

    sev = out.get("severity")
    if isinstance(sev, str):
        out["severity"] = sev.strip().upper()

    conf = out.get("confidence")
    if isinstance(conf, str):
        try:
            conf = float(conf.strip().rstrip("%"))
            if conf > 1.0:
                conf /= 100.0
        except ValueError:
            conf = None
    if isinstance(conf, (int, float)):
        if conf < 0.0 or conf > 1.0:
            flags.append(VerificationFlag.CONFIDENCE_OUT_OF_RANGE.value)
            conf = min(1.0, max(0.0, float(conf)))
        out["confidence"] = float(conf)
    elif conf is None and "confidence" in out:
        flags.append(VerificationFlag.CONFIDENCE_OUT_OF_RANGE.value)
        out["confidence"] = 0.0

    for key in ("sop_clause", "sop_clause_text", "regulation_ref",
                "regulation_excerpt", "gap_description", "remediation"):
        if out.get(key) is None:
            out[key] = ""
        elif not isinstance(out.get(key), str) and key in out:
            out[key] = str(out[key])

    return out, flags


# ---------------------------------------------------------------------------
# 4. Citation grounding
# ---------------------------------------------------------------------------

_CFR = re.compile(r"(?i)\b21\s*cfr\s*(?:part\s*)?(\d+)")
_ICH = re.compile(r"(?i)\bich\s*q\s*(\d+[a-z]?)")
_EMA = re.compile(r"(?i)\b(chmp|bwp|bmwp|ewp|cpmp)[\s/]*(\d+)")


def reg_keys(text: str) -> set[str]:
    """Extract regulation identity keys from a citation or a filename.

    Corpus metadata is document-level ("21 CFR Part 211 — FDA CGMP"), while
    citations are section-level ("21 CFR 211.192"), so matching has to happen
    on document identity rather than on the full string.
    """
    if not text:
        return set()
    keys: set[str] = set()

    # Match against the raw string AND its normalised form. Corpus source_file
    # values are filenames ("ich_q5e.pdf", "ema_chmp_437_04_rev1"), where
    # underscores would otherwise defeat every pattern below.
    for variant in (text, _norm(text)):
        for m in _CFR.finditer(variant):
            keys.add(f"cfr{m.group(1)}")
        for m in _ICH.finditer(variant):
            # ICH Q2(R1) -> ichq2 ; the revision suffix is not identity
            keys.add(f"ichq{re.sub(r'[a-z]+$', '', m.group(1).lower())}")
        for m in _EMA.finditer(variant):
            keys.add(f"{m.group(1).lower()}{m.group(2)}")

    # 21 CFR Part 211 cites sections as 211.192, so a bare 3-digit token next
    # to "cfr" is also a document-identity candidate.
    stem = _norm(text)
    if "cfr" in stem:
        for num in re.findall(r"\b(\d{3})\b", stem):
            keys.add(f"cfr{num}")
    return keys


def _containment(needle: set[str], haystack: set[str]) -> float:
    """Fraction of `needle` present in `haystack`.

    Containment, not Jaccard: the chunk is ~512 tokens and the excerpt ~20, so
    Jaccard would be near-zero even for a perfect quote.
    """
    if not needle:
        return 0.0
    return len(needle & haystack) / len(needle)


@dataclass
class Verification:
    status: VerificationStatus
    flags: list[str] = field(default_factory=list)
    grounding_score: Optional[float] = None
    grounded_source_file: Optional[str] = None


def verify_finding(
    finding: GapResult,
    clause_body: str,
    clause_section_id: str,
    reg_chunks: list[dict],
    extra_flags: Optional[list[str]] = None,
) -> Verification:
    """Check a finding against the chunks that were actually retrieved.

    Never mutates severity or confidence — flagging must not change a finding's
    substance, only disclose that it could not be verified.
    """
    flags: list[str] = list(extra_flags or [])

    # --- Tier 1: document identity ---------------------------------------
    cited = reg_keys(finding.regulation_ref)
    available: set[str] = set()
    for c in reg_chunks:
        available |= reg_keys(c.get("regulation_ref", ""))
        available |= reg_keys(c.get("source_file", ""))

    shared = {k for k in (cited & available) if any(ch.isdigit() for ch in k)}
    if not shared:
        flags.append(VerificationFlag.CITATION_NOT_IN_CORPUS.value)

    # --- Tier 2: excerpt fidelity ----------------------------------------
    score: Optional[float] = None
    source_file: Optional[str] = None
    ex_tokens = _tokens(finding.regulation_excerpt)

    if len(ex_tokens) < MIN_EXCERPT_TOKENS:
        flags.append(VerificationFlag.EXCERPT_TOO_SHORT.value)
    else:
        needle = _norm(finding.regulation_excerpt)
        best = 0.0
        for c in reg_chunks:
            chunk_text = c.get("text", "")
            if needle and needle in _norm(chunk_text):
                best, source_file = 1.0, c.get("source_file")
                break
            cont = _containment(ex_tokens, _tokens(chunk_text))
            if cont > best:
                best, source_file = cont, c.get("source_file")
        score = round(best, 4)
        if best < EXCERPT_CONTAINMENT_THRESHOLD:
            flags.append(VerificationFlag.EXCERPT_NOT_FOUND.value)
            source_file = None

    # --- Tier 3: SOP self-consistency ------------------------------------
    sop_tokens = _tokens(finding.sop_clause_text)
    if sop_tokens:
        if _containment(sop_tokens, _tokens(clause_body)) < EXCERPT_CONTAINMENT_THRESHOLD:
            flags.append(VerificationFlag.SOP_TEXT_NOT_FOUND.value)

    a, b = _norm_section(finding.sop_clause), _norm_section(clause_section_id)
    if a and b and a != b:
        flags.append(VerificationFlag.CLAUSE_ID_MISMATCH.value)

    # --- Tier 4: confidence ----------------------------------------------
    if finding.confidence < CONFIDENCE_THRESHOLD:
        flags.append(VerificationFlag.LOW_CONFIDENCE.value)

    # de-duplicate, preserve order
    seen: set[str] = set()
    ordered = [f for f in flags if not (f in seen or seen.add(f))]

    return Verification(
        status=(VerificationStatus.REQUIRES_HUMAN_VERIFICATION if ordered
                else VerificationStatus.VERIFIED),
        flags=ordered,
        grounding_score=score,
        grounded_source_file=source_file,
    )


FLAG_LABELS: dict[str, str] = {
    VerificationFlag.LOW_CONFIDENCE.value:
        f"Model confidence is below {CONFIDENCE_THRESHOLD:.0%}.",
    VerificationFlag.CITATION_NOT_IN_CORPUS.value:
        "The cited regulation is not among the documents in the indexed corpus.",
    VerificationFlag.EXCERPT_NOT_FOUND.value:
        "The quoted regulatory text could not be located in the retrieved corpus.",
    VerificationFlag.EXCERPT_TOO_SHORT.value:
        "The regulatory quote is too short to verify.",
    VerificationFlag.SOP_TEXT_NOT_FOUND.value:
        "The quoted SOP text does not match the clause it was taken from.",
    VerificationFlag.CLAUSE_ID_MISMATCH.value:
        "The reported clause number differs from the clause analysed.",
    VerificationFlag.INJECTION_SUSPECTED.value:
        "The source clause contained text shaped like system instructions.",
    VerificationFlag.CLAUSE_TRUNCATED.value:
        "The clause exceeded the analysis limit; later text was not assessed.",
    VerificationFlag.CONFIDENCE_OUT_OF_RANGE.value:
        "The model returned a confidence value outside 0.0–1.0.",
}
