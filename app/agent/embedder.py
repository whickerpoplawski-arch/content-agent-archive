"""Local Chinese text embedder: BAAI/bge-small-zh-v1.5 via ONNX Runtime CPU.

Selected in docs/memory-rag-selection.md: the ustc LLM gateway exposes no
embedding models (all /v1/embeddings 403), so embeddings run locally. The
Xenova ONNX export (91MB, 512-dim) lives in data/models/ and runs ~120ms per
4-text batch on CPU — no torch, no sentence-transformers, no network.

Pooling: CLS token + L2 normalization (bge standard). Outputs are deterministic
and directly comparable by cosine similarity.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger("agent.embedder")

MODEL_DIR = Path("data") / "models"
EMBED_DIM = 512

# bge query instruction: boosts retrieval for short queries vs long passages.
# https://huggingface.co/BAAI/bge-small-zh-v1.5
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class BGESmallZhEmbedder:
    """Thread-safe singleton-style embedder wrapping the ONNX model."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls) -> "BGESmallZhEmbedder":
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._init_session()
                cls._instance = inst
        return cls._instance

    def _init_session(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_path = MODEL_DIR / "bge-small-zh-v1.5.onnx"
        if not model_path.exists():
            raise FileNotFoundError(
                f"embedding model missing: {model_path} — see docs/memory-rag-selection.md"
            )
        self._tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
        self._tokenizer.no_padding()
        self._tokenizer.no_truncation()
        self._session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        """Embed texts -> (n, 512) float32, L2-normalized rows.

        ``is_query=True`` prepends the bge retrieval instruction (queries only;
        passages are embedded raw). Each sequence is padded to a fixed 512-len
        grid so results are independent of batch composition (padding-to-batch-
        max perturbs BERT CLS output slightly; fixed length keeps vectors
        deterministic regardless of what else is in the batch).
        """
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        prepared = [
            (QUERY_INSTRUCTION + t) if is_query else t for t in texts
        ]
        encodings = self._tokenizer.encode_batch(prepared)
        max_len = 512  # fixed grid: batch-composition-independent vectors
        batch = len(encodings)
        ids = np.zeros((batch, max_len), dtype=np.int64)
        mask = np.zeros((batch, max_len), dtype=np.int64)
        token_types = np.zeros((batch, max_len), dtype=np.int64)
        for i, enc in enumerate(encodings):
            n = min(len(enc.ids), max_len)
            ids[i, :n] = enc.ids[:max_len]
            mask[i, :n] = 1
        hidden = self._session.run(
            None,
            {
                "input_ids": ids,
                "attention_mask": mask,
                "token_type_ids": token_types,
            },
        )[0]
        cls = hidden[:, 0].astype(np.float32)
        norms = np.linalg.norm(cls, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return cls / norms


def get_embedder() -> BGESmallZhEmbedder:
    """Process-wide shared embedder (model loads once)."""
    return BGESmallZhEmbedder()
