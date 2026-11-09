"""Central configuration.

Every tunable value (API keys, model names, chunk sizes, retrieval depths,
agent limits) comes from environment variables or a `.env` file. Nothing
bid-specific lives in code: field definitions, query synonyms and document
classification rules are YAML files under `config/`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ---------------- LLM (any OpenAI-compatible endpoint) ----------------
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"),
    )
    llm_model: str = "openai/gpt-oss-120b"
    # Comma-separated. Used when the primary model is rate limited / failing.
    llm_fallback_models: str = "openai/gpt-oss-20b,qwen/qwen3.8-27b"
    llm_timeout: float = 90.0
    llm_max_attempts: int = 6
    llm_json_mode: bool = True
    llm_reasoning_effort: str = "low"  # only sent to reasoning models (gpt-oss)
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 2500
    # Client-side tokens-per-minute budget per model (0 = disabled).
    # Groq free tier is 8k TPM; set 0 for OpenAI / local Ollama.
    llm_tpm_limit: int = 8000
    llm_cache_enabled: bool = True
    # quality-critical agents wait up to this many seconds for the primary model before using a fallback
    llm_primary_max_wait: float = 70.0
    llm_price_input_per_m: float = 0.15
    llm_price_output_per_m: float = 0.60

    # ---------------- Embeddings / reranker ----------------
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    # Prefix for queries (BGE v1.5 recommends an instruction for short queries).
    # "auto" -> BGE instruction for bge-* models, nothing otherwise.
    embedding_query_prefix: str = "auto"
    embedding_batch_size: int = 32
    reranker_model: str = "BAAI/bge-reranker-base"
    device: str = "auto"  # auto | cuda | cpu

    # ---------------- Ingestion / chunking ----------------
    chunk_target_words: int = 180
    chunk_overlap_words: int = 30
    chunk_min_words: int = 20
    table_rows_per_chunk: int = 8
    header_footer_min_ratio: float = 0.5
    ocr_enabled: bool = True
    ocr_engine: str = "easyocr"  # easyocr | tesseract
    ocr_min_chars: int = 30

    # ---------------- Retrieval ----------------
    top_k: int = 5
    retrieve_candidates: int = 40  # per retriever, before fusion
    rerank_candidates: int = 30  # fused candidates sent to the cross-encoder
    rrf_k: int = 60
    default_search_mode: str = "hybrid_rerank"  # bm25 | dense | hybrid | hybrid_rerank

    # ---------------- Agents ----------------
    agent_max_retries: int = 2
    agent_max_concurrency: int = 4
    evidence_per_field: int = 3
    evidence_pool_max: int = 13
    evidence_max_chars: int = 1200
    addendum_max_chars: int = 7000
    validator_use_llm: bool = True
    qa_evidence_k: int = 6
    semantic_cache_threshold: float = 0.95

    # ---------------- Paths ----------------
    data_dir: str = "data"
    index_dir: str = "index_store"
    output_dir: str = "outputs"
    cache_dir: str = ".cache"
    log_dir: str = "logs"
    fields_config: str = "config/fields.yaml"
    synonyms_config: str = "config/synonyms.yaml"
    doc_types_config: str = "config/doc_types.yaml"

    # ------------------------------------------------------------------
    def path(self, value: str) -> Path:
        """Resolve a configured path relative to the project root."""
        p = Path(value)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @property
    def resolved_device(self) -> str:
        if self.device != "auto":
            return self.device
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # pragma: no cover - torch missing
            return "cpu"

    @property
    def query_prefix(self) -> str:
        if self.embedding_query_prefix != "auto":
            return self.embedding_query_prefix
        name = self.embedding_model.lower()
        if "bge" in name and "m3" not in name:
            return "Represent this sentence for searching relevant passages: "
        return ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
