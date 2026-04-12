"""
Corpus embedder — loads regulatory PDFs, chunks by token count, embeds with
OpenAI text-embedding-3-large, and persists to a local ChromaDB collection.

Usage:
    python -m src.ingest.embedder --source corpus/
    python -m src.ingest.embedder --source corpus/ --reset
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import tiktoken
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from tqdm import tqdm

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Corpus metadata — maps canonical filename → human-readable regulation name
# ---------------------------------------------------------------------------
CORPUS_METADATA: dict[str, str] = {
    "21_cfr_part_211.pdf": "21 CFR Part 211 — FDA Current Good Manufacturing Practice",
    "21_cfr_part_600_610.pdf": "21 CFR Part 600-610 — FDA Biologics Regulations",
    "ich_q5e.pdf": "ICH Q5E — Comparability of Biotechnological/Biological Products",
    "ich_q10.pdf": "ICH Q10 — Pharmaceutical Quality System",
    "ich_q11.pdf": "ICH Q11 — Development and Manufacture of Drug Substances (Biologics)",
}

COLLECTION_NAME = "regulatory_corpus"
CHUNK_SIZE = 512      # tokens
CHUNK_OVERLAP = 64    # tokens
BATCH_SIZE = 100      # docs per ChromaDB upsert call


class CorpusEmbedder:
    def __init__(
        self,
        source_dir: Path,
        chroma_path: Path,
        collection_name: str = COLLECTION_NAME,
        reset: bool = False,
    ) -> None:
        self.source_dir = source_dir
        self.chroma_path = chroma_path
        self.collection_name = collection_name
        self.reset = reset
        self._enc = tiktoken.get_encoding("cl100k_base")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Ingest all PDFs and return manifest data."""
        embeddings = OpenAIEmbeddings(
            model="openai/text-embedding-3-large",
            openai_api_key=os.environ["OPENROUTER_API_KEY"],
            openai_api_base="https://openrouter.ai/api/v1",
        )

        if self.reset:
            import chromadb
            client = chromadb.PersistentClient(path=str(self.chroma_path))
            try:
                client.delete_collection(self.collection_name)
                logger.info("Deleted existing collection '%s'", self.collection_name)
            except Exception:
                pass

        vectorstore = Chroma(
            collection_name=self.collection_name,
            embedding_function=embeddings,
            persist_directory=str(self.chroma_path),
        )

        raw_docs = self._load_pdfs()
        if not raw_docs:
            raise RuntimeError(f"No PDFs could be loaded from {self.source_dir}")

        all_texts: list[str] = []
        all_metadatas: list[dict] = []
        all_ids: list[str] = []
        source_stats: list[dict] = []

        for doc in raw_docs:
            chunks = self._chunk_tokens(doc["text"])
            for i, chunk in enumerate(chunks):
                doc_id = f"{doc['source_file']}::chunk_{i:04d}"
                all_texts.append(chunk)
                all_metadatas.append({
                    "source_file": doc["source_file"],
                    "regulation_ref": doc["regulation_ref"],
                    "page_count": doc["page_count"],
                    "chunk_index": i,
                })
                all_ids.append(doc_id)
            source_stats.append({
                "file": doc["source_file"],
                "regulation_ref": doc["regulation_ref"],
                "page_count": doc["page_count"],
                "chunk_count": len(chunks),
                "status": "ok",
            })
            logger.info(
                "  %s — %d pages, %d chunks",
                doc["source_file"], doc["page_count"], len(chunks),
            )

        # Add missing files to manifest
        for filename in CORPUS_METADATA:
            if not (self.source_dir / filename).exists():
                source_stats.append({"file": filename, "status": "missing"})

        logger.info(
            "Embedding %d chunks across %d sources …", len(all_texts), len(raw_docs)
        )
        self._batch_upsert(vectorstore, all_texts, all_metadatas, all_ids)

        manifest = {
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "chroma_path": str(self.chroma_path),
            "collection": self.collection_name,
            "sources": source_stats,
            "total_chunks": len(all_texts),
        }
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        manifest_path = out_dir / "corpus_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        logger.info("Manifest written to %s", manifest_path)
        logger.info("Ingestion complete — %d chunks stored.", len(all_texts))
        return manifest

    # ------------------------------------------------------------------
    # PDF loading
    # ------------------------------------------------------------------

    def _load_pdfs(self) -> list[dict]:
        docs: list[dict] = []
        pdf_files = sorted(self.source_dir.glob("*.pdf"))
        if not pdf_files:
            logger.warning("No PDF files found in %s", self.source_dir)
            return docs

        for pdf_path in pdf_files:
            try:
                doc = fitz.open(str(pdf_path))
                pages_text = []
                for page in doc:
                    pages_text.append(page.get_text())
                full_text = "\n".join(pages_text)
                docs.append({
                    "source_file": pdf_path.name,
                    "regulation_ref": CORPUS_METADATA.get(
                        pdf_path.name, pdf_path.stem
                    ),
                    "text": full_text,
                    "page_count": len(doc),
                })
                logger.info("Loaded %s (%d pages)", pdf_path.name, len(doc))
            except Exception as exc:
                logger.warning("Could not load %s: %s — skipping", pdf_path.name, exc)

        return docs

    # ------------------------------------------------------------------
    # Token-aware chunker
    # ------------------------------------------------------------------

    def _chunk_tokens(
        self, text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
    ) -> list[str]:
        tokens = self._enc.encode(text)
        chunks: list[str] = []
        start = 0
        while start < len(tokens):
            end = min(start + size, len(tokens))
            chunks.append(self._enc.decode(tokens[start:end]))
            if end == len(tokens):
                break
            start += size - overlap
        return chunks

    # ------------------------------------------------------------------
    # Batched upsert with rate-limit protection
    # ------------------------------------------------------------------

    def _batch_upsert(
        self,
        vectorstore: Chroma,
        texts: list[str],
        metadatas: list[dict],
        ids: list[str],
    ) -> None:
        for i in tqdm(range(0, len(texts), BATCH_SIZE), desc="Embedding batches"):
            batch_texts = texts[i : i + BATCH_SIZE]
            batch_meta = metadatas[i : i + BATCH_SIZE]
            batch_ids = ids[i : i + BATCH_SIZE]
            vectorstore.add_texts(
                texts=batch_texts, metadatas=batch_meta, ids=batch_ids
            )
            if i + BATCH_SIZE < len(texts):
                time.sleep(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest regulatory corpus PDFs into ChromaDB"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("corpus"),
        help="Directory containing regulatory PDFs (default: corpus/)",
    )
    parser.add_argument(
        "--chroma-path",
        type=Path,
        default=Path(os.environ.get("CHROMA_PATH", "./chroma_db")),
        help="ChromaDB persist directory (default: ./chroma_db)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing collection before re-ingesting",
    )
    args = parser.parse_args()

    embedder = CorpusEmbedder(
        source_dir=args.source,
        chroma_path=args.chroma_path,
        reset=args.reset,
    )
    embedder.run()


if __name__ == "__main__":
    main()
