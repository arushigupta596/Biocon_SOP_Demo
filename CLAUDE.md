# SOP Compliance Engine — Biocon Demo

## Project Purpose
RAG pipeline that detects regulatory compliance gaps in pharmaceutical SOPs against
EMA, FDA, and ICH guidelines. Built for live demo with Biocon Biologics.

The system ingests regulatory PDFs into a local Chroma vector store, then runs
clause-level comparison against Biocon SOP documents, producing structured gap
findings and an audit-ready DOCX report.

---

## Tech Stack
- Python 3.11+
- LangChain — document loading and chain orchestration
- ChromaDB (local) — vector store, no cloud
- OpenAI text-embedding-3-large — embeddings
- Claude claude-sonnet-4-20250514 — gap detection via structured JSON output only
- python-docx + Jinja2 — report generation
- PyMuPDF (fitz) — PDF parsing
- tiktoken — token-aware chunking
- Pydantic v2 — data validation and schema
- pytest — all tests

---

## Key Commands

```bash
# Ingest regulatory corpus
python -m src.ingest.embedder --source corpus/

# Scan a single SOP
python -m src.gap_engine.detector --sop sops/BC-MFG-UC-047.docx

# Scan all SOPs in parallel
python -m src.gap_engine.detector --all --sops sops/

# Generate gap report
python -m src.report.generator --registry output/gap_registry.json

# Run full test suite
pytest tests/ -v

# Pre-demo smoke test (run the night before every demo)
pytest tests/test_demo_gaps.py -v
```

---

## Architecture Rules

1. Gap detector MUST return valid JSON matching GapResult schema — never prose
2. Chunk size is fixed at 512 tokens with 64 token overlap — do not change without full re-index
3. All EMA/FDA/ICH source PDFs go in corpus/ — never put SOPs in corpus/
4. Vector store is local Chroma at ./chroma_db — no cloud vector store
5. The Claude API call in detector.py must use temperature=0 for deterministic output
6. Every gap finding must include: sop_clause, regulation_ref, gap_description, severity, remediation, confidence
7. Report generator reads only from output/gap_registry.json — never calls the API directly
8. WebFetch is disabled — all data is local

---

## Gap Severity Definitions

| Severity | Meaning |
|----------|---------|
| CRITICAL | Missing required element; will cause regulatory non-conformance at inspection |
| MAJOR    | Deficient element; likely cited at FDA/EMA inspection |
| MINOR    | Improvement recommended; low regulatory risk |

---

## Corpus — Regulatory PDFs (must be present in corpus/ before /ingest)

| File | Regulation | Relevance |
|------|-----------|-----------|
| ema_chmp_437_04_rev1.pdf | EMA CHMP/437/04 Rev1 | Master biosimilar guideline |
| ema_quality_issues_bwp_247713.pdf | EMA BWP/247713 | Biosimilar quality / manufacturing |
| ema_nonclinical_clinical_bmwp_42832.pdf | EMA BMWP/42832 Rev1 | Non-clinical and clinical issues |
| ema_mab_nonclinical_bmwp_184161.pdf | EMA BMWP/184161 | mAb-specific non-clinical/clinical |
| ema_immunogenicity_bmwp_14327.pdf | EMA BMWP/14327 | Immunogenicity assessment |
| ema_immunogenicity_mab_bmwp_86289.pdf | EMA BMWP/86289 | mAb immunogenicity |
| ich_q5e.pdf | ICH Q5E | Comparability after manufacturing changes |
| ich_q10.pdf | ICH Q10 | Pharmaceutical Quality System |
| ich_q11.pdf | ICH Q11 | Drug substance development (biologics) |
| ich_q2r1.pdf | ICH Q2(R1) | Analytical method validation |
| 21_cfr_part_211.pdf | 21 CFR Part 211 | FDA GMP |
| 21_cfr_part_600_610.pdf | 21 CFR 600-610 | FDA biologics regulations |

---

## SOPs — Biocon Files (must be in sops/)

| File | SOP ID | Pre-scripted Gaps |
|------|--------|-------------------|
| BC-MFG-UC-047_Upstream_Cell_Culture_mAb.docx | BC-MFG-UC-047 | §7.3 comparability protocol, §6.2 glycosylation CQA |
| BC-QC-BR-012_Batch_Record_Review.docx | BC-QC-BR-012 | §5.2 review timeline, §6.1 OOS procedure |
| BC-RA-IM-008_Immunogenicity_Risk_Assessment.docx | BC-RA-IM-008 | §5.1 testing plan, §5.3 ADA assay validation |
| BC-AN-MV-031_Analytical_Method_Validation_ProteinA_HPLC.docx | BC-AN-MV-031 | §6.3 system suitability, §8 method transfer |

---

## Pre-scripted Demo Gaps (verify ALL fire before every demo)

These are the exact gaps that must surface during the live Biocon demo.
Run `/verify-demo` the night before and confirm confidence >= 0.80 for each.

```
GAP 1 — CRITICAL
  SOP:  BC-MFG-UC-047 Section 7.3 (Process Change Control)
  Ref:  EMA CHMP/437/04 Rev1 §5.2.3
  Desc: No comparability protocol defined for post-approval manufacturing changes

GAP 2 — CRITICAL
  SOP:  BC-MFG-UC-047 Section 6.2 (Acceptance Criteria)
  Ref:  EMA BWP/247713 §5.3.2
  Desc: Glycosylation profile not listed as a Critical Quality Attribute (CQA)

GAP 3 — CRITICAL
  SOP:  BC-QC-BR-012 Section 5.2 (Review Timeline)
  Ref:  21 CFR 211.192
  Desc: Batch record review frequency and completion deadline not specified

GAP 4 — CRITICAL
  SOP:  BC-AN-MV-031 Section 6.3 (System Suitability)
  Ref:  ICH Q2(R1) §2 + 21 CFR 211.194(a)
  Desc: System suitability criteria not defined; no pass/fail parameters before sample analysis
```

---

## File Outputs

| File | Description |
|------|-------------|
| chroma_db/ | Local Chroma vector store (auto-created by /ingest) |
| output/corpus_manifest.json | Chunk counts and metadata per source PDF |
| output/gap_registry_{sop_id}.json | Per-SOP gap findings |
| output/gap_registry.json | Aggregated master registry (all SOPs) |
| output/gap_report_{date}.docx | Audit-ready Word report |

---

## Demo Run Order

1. `claude /ingest` — build corpus (do this once; skip if chroma_db/ already exists)
2. `claude /scan-sop --file sops/BC-MFG-UC-047.docx` — live gap detection, Act 2
3. `claude /generate-report` — render report, Act 3
4. `claude /verify-demo` — night-before smoke test

---

## Environment Variables Required

```bash
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...        # for text-embedding-3-large
CHROMA_PATH=./chroma_db      # defaults to this if not set
```
