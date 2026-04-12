"""
Copy and rename regulatory PDFs from Data/ to corpus/ with canonical filenames.

Usage:
    python scripts/setup_corpus.py
"""
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

MAPPINGS = [
    (
        "Data/21 CFR Part 211 (up to date as of 4-09-2026).pdf",
        "corpus/21_cfr_part_211.pdf",
    ),
    (
        "Data/21 CFR Part 610 (up to date as of 4-09-2026).pdf",
        "corpus/21_cfr_part_600_610.pdf",
    ),
    (
        "Data/Q10 Guideline.pdf",
        "corpus/ich_q10.pdf",
    ),
    (
        "Data/Q11 Guideline.pdf",
        "corpus/ich_q11.pdf",
    ),
    (
        "Data/Q5E Guideline.pdf",
        "corpus/ich_q5e.pdf",
    ),
]

EXPECTED_MISSING = []


def main() -> None:
    corpus_dir = BASE / "corpus"
    corpus_dir.mkdir(exist_ok=True)

    copied = 0
    skipped = 0
    for src_rel, dst_rel in MAPPINGS:
        src = BASE / src_rel
        dst = BASE / dst_rel
        if not src.exists():
            print(f"  MISSING source: {src_rel}")
            skipped += 1
            continue
        if dst.exists():
            print(f"  ALREADY EXISTS: {dst_rel} — skipping")
            skipped += 1
            continue
        shutil.copy2(src, dst)
        print(f"  OK  {src_rel}  →  {dst_rel}")
        copied += 1

    print(f"\nCopied {copied} files, skipped {skipped}.")



if __name__ == "__main__":
    main()
