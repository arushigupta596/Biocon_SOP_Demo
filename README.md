# Biocon SOP Compliance Engine

**Intelligent regulatory gap detection for pharmaceutical SOPs — powered by RAG and Claude AI**

---

## What This System Does

Pharmaceutical SOPs must comply with a dense web of FDA, ICH, and EMA regulations. Manual review is slow, inconsistent, and error-prone. Auditors and QA teams spend weeks cross-referencing documents that run hundreds of pages, often missing gaps that become citations at inspection.

This engine automates that process end-to-end. Given any SOP document, it:

1. Parses the document into numbered clauses
2. Retrieves the most semantically relevant regulatory passages using vector search
3. Sends each clause + regulatory context to Claude for expert-level gap analysis
4. Returns structured findings with severity, regulatory citation, and specific remediation steps
5. Generates an audit-ready Word report

The system is built specifically for Biocon Biologics' mAb manufacturing, quality control, and regulatory affairs SOPs, with pre-validated findings mapped to known compliance gaps across four critical documents.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          Streamlit UI (app.py)                       │
│   Upload SOP  ──►  Run Scan  ──►  View Findings  ──►  Download DOCX  │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
           ┌───────────────▼────────────────┐
           │     Gap Detection Engine        │
           │   src/gap_engine/detector.py    │
           │                                 │
           │  1. Parse SOP into clauses      │
           │  2. Semantic search (ChromaDB)  │
           │  3. Claude gap analysis (JSON)  │
           │  4. Pydantic validation          │
           └───────┬───────────────┬─────────┘
                   │               │
    ┌──────────────▼──┐     ┌──────▼──────────────┐
    │  ChromaDB        │     │  OpenRouter API       │
    │  (local vector   │     │  Embeddings:          │
    │   store)         │     │  text-embedding-3-    │
    │                  │     │  large                │
    │  regulatory_     │     │                       │
    │  corpus          │     │  LLM:                 │
    │  collection      │     │  claude-sonnet-4-5    │
    └──────────────────┘     └───────────────────────┘
                   │
    ┌──────────────▼──────────────────┐
    │  Regulatory Corpus (corpus/)     │
    │  5 PDFs, pre-indexed             │
    │  21 CFR Part 211                 │
    │  21 CFR Part 600–610             │
    │  ICH Q5E / Q10 / Q11             │
    └──────────────────────────────────┘
```

**Data flow for a single scan:**

```
SOP (.docx / .pdf)
        │
        ▼
Clause extraction (regex + heading detection)
        │
        ├── For each clause ──► Top-K semantic retrieval from ChromaDB
        │                               │
        │                        Regulatory chunks (K=8, score ≥ 0.30)
        │                               │
        └───────────────────────────────┤
                                        ▼
                         Claude prompt (clause + regulatory context)
                                        │
                                        ▼
                         JSON array of GapResult objects
                                        │
                         Pydantic v2 validation + deduplication
                                        │
                                        ▼
                    output/gap_registry_{sop_id}.json
                    output/gap_registry.json (aggregated)
```

---

## Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| AI / LLM | Claude claude-sonnet-4-5 via OpenRouter | Gap analysis, structured JSON output |
| Embeddings | OpenAI text-embedding-3-large via OpenRouter | Semantic vector search |
| Vector store | ChromaDB (local) | Regulatory corpus retrieval |
| RAG framework | LangChain | Document loading, chain orchestration |
| PDF parsing | PyMuPDF (fitz) | Extracting text from regulatory PDFs and SOP PDFs |
| DOCX parsing | python-docx | Parsing SOP Word documents |
| Report generation | python-docx + Jinja2 | Audit-ready Word output |
| Chunking | tiktoken (cl100k_base) | Token-aware 512/64 chunk/overlap splitting |
| Data contracts | Pydantic v2 | Schema enforcement on all LLM outputs |
| API gateway | OpenRouter | Unified endpoint for Claude + OpenAI embeddings |
| UI | Streamlit | Demo interface |
| Testing | pytest | Pre-demo smoke test suite |

---

## Repository Structure

```
Biocon/
├── app.py                          # Streamlit UI (717 lines)
├── requirements.txt                # Python dependencies
│
├── src/
│   ├── schemas.py                  # Pydantic data contracts (GapResult, SOPScanResult, GapRegistry)
│   ├── ingest/
│   │   └── embedder.py             # PDF ingestion → ChromaDB (256 lines)
│   ├── gap_engine/
│   │   └── detector.py             # Core RAG + Claude pipeline (462 lines)
│   └── report/
│       └── generator.py            # DOCX report renderer (482 lines)
│
├── corpus/                         # Regulatory PDFs (FDA + ICH, ~1.7 MB total)
│   ├── 21_cfr_part_211.pdf
│   ├── 21_cfr_part_600_610.pdf
│   ├── ich_q5e.pdf
│   ├── ich_q10.pdf
│   └── ich_q11.pdf
│
├── sops/                           # Biocon SOP documents (4 files)
│   ├── BC-MFG-UC-047_Upstream_Cell_Culture_mAb.docx
│   ├── BC-QC-BR-012_Batch_Record_Review.docx
│   ├── BC-RA-IM-008_Immunogenicity_Risk_Assessment.docx
│   └── BC-AN-MV-031_Analytical_Method_Validation_ProteinA_HPLC.docx
│
├── chroma_db/                      # Pre-built vector store (6.7 MB, ready to use)
├── output/                         # Generated at runtime (registries, reports)
├── tests/                          # pytest suite — pre-demo smoke tests
└── .streamlit/
    ├── config.toml                 # Theme and server settings
    └── secrets.toml                # API keys (not committed — set in Cloud dashboard)
```

---

## Data Model

Every compliance finding is a validated `GapResult` object:

```python
class GapResult(BaseModel):
    sop_id:            str      # e.g. "BC-MFG-UC-047"
    sop_clause:        str      # e.g. "§7.3"
    sop_clause_text:   str      # Verbatim deficient text from the SOP
    regulation_ref:    str      # e.g. "21 CFR 211.192"
    regulation_excerpt:str      # Retrieved regulatory passage
    gap_description:   str      # Plain-English compliance gap
    severity:          Severity # CRITICAL | MAJOR | MINOR
    remediation:       str      # Actionable fix
    confidence:        float    # Model confidence 0.0–1.0
```

Severity is defined against inspection risk:

| Severity | Definition |
|----------|-----------|
| **CRITICAL** | Missing required element — will cause regulatory non-conformance at FDA/EMA inspection |
| **MAJOR** | Deficient element — likely cited at inspection |
| **MINOR** | Improvement recommended — low regulatory risk |

---

## Pre-Validated Demo Gaps

Four critical findings are hard-validated in the test suite (`tests/test_demo_gaps.py`) and must fire with confidence ≥ 0.80 before every client demo:

| Gap | SOP | Clause | Regulation | Description |
|-----|-----|--------|-----------|-------------|
| 1 | BC-MFG-UC-047 | §7.3 | 21 CFR Part 211 | No comparability protocol for post-approval manufacturing changes |
| 2 | BC-MFG-UC-047 | §6.2 | ICH Q11 | Glycosylation profile not listed as a Critical Quality Attribute |
| 3 | BC-QC-BR-012 | §5.2 | 21 CFR 211.192 | Batch record review frequency and completion deadline not specified |
| 4 | BC-AN-MV-031 | §6.3 | ICH Q2(R1) | System suitability criteria not defined before sample analysis |

---

## Engineering Decisions

**Why ChromaDB local instead of a cloud vector store?**
The regulatory corpus is static (PDFs do not change between demos) and the vector store is small enough (6.7 MB) to commit directly to the repository. This eliminates a cloud dependency, removes latency, and keeps all data on-premise for IP reasons.

**Why OpenRouter instead of direct Anthropic/OpenAI SDKs?**
OpenRouter provides a single API endpoint and key for both Claude (LLM) and OpenAI embeddings. This simplifies secrets management and makes it straightforward to swap models without code changes.

**Why temperature=0 on the Claude call?**
Gap detection must be deterministic and reproducible. The same SOP must produce the same findings across runs so that audit trails are consistent and regression tests pass reliably.

**Why structured JSON output enforced via a system prompt contract?**
LLM prose output cannot be validated by Pydantic. The OUTPUT CONTRACT system prompt instructs Claude to return only a raw JSON array matching `GapResult` schema. Every response is parsed and validated before being written to the registry — malformed output is logged and skipped, never silently accepted.

**Content-hash caching**
SOP file bytes are SHA-256 hashed on upload. If the hash matches a previous scan in `output/scan_cache.json`, results are loaded instantly from the cached `gap_registry_{sop_id}.json` — no API calls are made. This makes repeat demos instant and cost-free.

---

## Running Locally

**Prerequisites:** Python 3.11+, an OpenRouter API key

```bash
# 1. Clone and install dependencies
git clone https://github.com/arushigupta596/Biocon_SOP_Demo.git
cd Biocon_SOP_Demo
pip install -r requirements.txt

# 2. Set your API key
cp .env.example .env
# Edit .env and fill in OPENROUTER_API_KEY

# 3. Launch the UI
streamlit run app.py
```

The vector store (`chroma_db/`) is pre-built and committed — no ingestion step needed.

**To re-ingest the corpus from scratch** (only needed if PDFs change):

```bash
python -m src.ingest.embedder --source corpus/
```

**To run the pre-demo smoke test:**

```bash
pytest tests/test_demo_gaps.py -v
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key — used for both embeddings and Claude |
| `CHROMA_PATH` | No | Path to ChromaDB directory. Defaults to `./chroma_db` |

---

## Streamlit Cloud Deployment

The app is deployed at: [share.streamlit.io](https://share.streamlit.io) (connect repo `arushigupta596/Biocon_SOP_Demo`)

Secrets are configured via the Streamlit Cloud dashboard under **Advanced settings → Secrets**:

```toml
OPENROUTER_API_KEY = "sk-or-..."
CHROMA_PATH = "./chroma_db"
```

---

## Demo Run Order

| Step | Command (UI) | What Happens |
|------|-------------|-------------|
| 1 | Upload SOP → Run Compliance Scan | Parses SOP, runs RAG pipeline, displays gap cards |
| 2 | Download Audit Report | Generates and downloads audit-ready DOCX |
| 3 | Gap Report page | Shows cross-SOP summary table |
| 4 | Run Tests | Executes smoke test suite, confirms all critical gaps fire |

---

## Cost Profile

Each full SOP scan (all 4 documents, ~150 clauses total) makes approximately:
- **~150 embedding calls** (text-embedding-3-large) — retrieval context per clause
- **~150 Claude calls** (claude-sonnet-4-5, 4K tokens max per call) — gap analysis

Estimated API cost per full demo run: **< $1.00** via OpenRouter.

Repeat scans of previously uploaded files cost **$0.00** due to content-hash caching.

---

## Ingested Files for Gap Analysis

### Regulatory Corpus — Reference Documents (vector-indexed)

These PDFs are chunked at 512 tokens (64-token overlap), embedded with `text-embedding-3-large`, and stored in ChromaDB. During a scan, the top-8 most semantically relevant chunks are retrieved per SOP clause and passed as regulatory context to Claude.

| File | Regulation | Scope | Pages | Chunks Indexed |
|------|-----------|-------|-------|---------------|
| `21_cfr_part_211.pdf` | 21 CFR Part 211 — FDA Current Good Manufacturing Practice | GMP requirements for finished pharmaceuticals: equipment, records, lab controls, batch review | 29 | 42 |
| `21_cfr_part_600_610.pdf` | 21 CFR Part 600–610 — FDA Biologics Regulations | Licensing, manufacturing, and testing requirements specific to biological products | 25 | 38 |
| `ich_q11.pdf` | ICH Q11 — Development and Manufacture of Drug Substances (Biologics) | CQA definition, control strategy, comparability for biotech drug substances | 30 | 37 |
| `ich_q10.pdf` | ICH Q10 — Pharmaceutical Quality System | Quality system elements, process performance, change management | 21 | 19 |
| `ich_q5e.pdf` | ICH Q5E — Comparability of Biotechnological/Biological Products | Comparability protocols after manufacturing changes for biologics | 16 | 18 |

**Total indexed:** 154 chunks across 121 pages of regulatory text

---

### Biocon SOPs — Documents Scanned for Gaps

These are the Biocon Biologics SOP documents that the engine analyses against the regulatory corpus. Each document is parsed into numbered clauses; every clause is individually assessed.

| File | SOP ID | Domain | Clauses Scanned | Gaps Found |
|------|--------|--------|----------------|-----------|
| `BC-MFG-UC-047_Upstream_Cell_Culture_mAb.docx` | BC-MFG-UC-047 | Upstream Cell Culture — mAb manufacturing process control and CQA management | 14 | 8 |
| `BC-QC-BR-012_Batch_Record_Review.docx` | BC-QC-BR-012 | Quality Control — batch record review procedures, OOS handling, timelines | 11 | 22 |
| `BC-RA-IM-008_Immunogenicity_Risk_Assessment.docx` | BC-RA-IM-008 | Regulatory Affairs — immunogenicity risk assessment, ADA testing, patient impact | 11 | 11 |
| `BC-AN-MV-031_Analytical_Method_Validation_ProteinA_HPLC.docx` | BC-AN-MV-031 | Analytical — Protein A HPLC method validation, system suitability, method transfer | 15 | 14 |

**Total across all SOPs:** 51 clauses scanned, 55 gaps identified

---

*Built by EMB Global for Biocon Biologics — April 2026*
