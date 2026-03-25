"""Local embeddings via MLX + BGE-base on Apple Silicon."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mlx.core as mx
import numpy as np
from huggingface_hub import snapshot_download
from mlx import nn
from opentelemetry import trace
from tokenizers import Tokenizer

from theo.config import get_settings

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

if TYPE_CHECKING:
    from numpy.typing import NDArray

_BATCH_SIZE = 64

# ---------------------------------------------------------------------------
# HuggingFace BERT key → our model key mapping
# ---------------------------------------------------------------------------
_EMBED_KEY_MAP: dict[str, str] = {
    "bert.embeddings.word_embeddings": "word_emb",
    "bert.embeddings.position_embeddings": "pos_emb",
    "bert.embeddings.token_type_embeddings": "tok_emb",
    "bert.embeddings.LayerNorm": "norm",
}

_LAYER_KEY_MAP: dict[str, str] = {
    "attention.self.query": "attn.query_proj",
    "attention.self.key": "attn.key_proj",
    "attention.self.value": "attn.value_proj",
    "attention.output.dense": "attn.out_proj",
    "attention.output.LayerNorm": "ln1",
    "intermediate.dense": "ff.layers.0",
    "output.dense": "ff.layers.2",
    "output.LayerNorm": "ln2",
}

_LAYER_RE = re.compile(r"bert\.encoder\.layer\.(\d+)\.(.+)")


def _map_hf_key(hf_key: str) -> str | None:
    """Map a HuggingFace BERT weight key to our model's parameter path.

    Returns ``None`` for keys we don't use (pooler, cls head).
    """
    for hf_prefix, our_prefix in _EMBED_KEY_MAP.items():
        if hf_key.startswith(hf_prefix):
            return our_prefix + hf_key[len(hf_prefix) :]

    m = _LAYER_RE.match(hf_key)
    if m:
        idx, rest = m.group(1), m.group(2)
        for hf_sub, our_sub in _LAYER_KEY_MAP.items():
            if rest.startswith(hf_sub):
                return f"layers.{idx}.{our_sub}{rest[len(hf_sub) :]}"

    return None


# ---------------------------------------------------------------------------
# Minimal BERT encoder — just enough for BGE-base inference
# ---------------------------------------------------------------------------


class _BertEmbedding(nn.Module):
    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        v: int = config["vocab_size"]
        d: int = config["hidden_size"]
        h: int = config["num_attention_heads"]
        n_layers: int = config["num_hidden_layers"]
        mid: int = config["intermediate_size"]
        max_pos: int = config.get("max_position_embeddings", 512)
        n_types: int = config.get("type_vocab_size", 2)

        self.word_emb = nn.Embedding(v, d)
        self.pos_emb = nn.Embedding(max_pos, d)
        self.tok_emb = nn.Embedding(n_types, d)
        self.norm = nn.LayerNorm(d)
        self.layers = [_TransformerBlock(d, h, mid) for _ in range(n_layers)]

    def __call__(self, input_ids: mx.array, attention_mask: mx.array) -> mx.array:
        seq_len = input_ids.shape[1]
        pos = mx.arange(seq_len)
        x = self.word_emb(input_ids) + self.pos_emb(pos) + self.tok_emb(mx.zeros_like(input_ids))
        x = self.norm(x)
        mask = attention_mask[:, None, None, :]
        for layer in self.layers:
            x = layer(x, mask)
        # CLS pooling
        return x[:, 0, :]


class _TransformerBlock(nn.Module):
    def __init__(self, d: int, h: int, mid: int) -> None:
        super().__init__()
        self.attn = nn.MultiHeadAttention(d, h)
        self.ln1 = nn.LayerNorm(d)
        self.ff = nn.Sequential(nn.Linear(d, mid), nn.GELU(), nn.Linear(mid, d))
        self.ln2 = nn.LayerNorm(d)

    def __call__(self, x: mx.array, mask: mx.array) -> mx.array:
        x = self.ln1(x + self.attn(x, x, x, mask=mask))
        return self.ln2(x + self.ff(x))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class Embedder:
    """Thread-safe, lazily-loaded embedding model.

    All public methods are async and safe to call from the event loop —
    heavy MLX inference runs in a background thread via ``asyncio.to_thread``.
    """

    def __init__(self) -> None:
        self._model: _BertEmbedding | None = None
        self._tokenizer: Tokenizer | None = None
        self._lock = threading.Lock()

    # -- lazy loading (runs in worker thread) --------------------------------

    def _ensure_loaded(self) -> tuple[_BertEmbedding, Tokenizer]:
        """Double-checked locking: fast path avoids the lock entirely."""
        if self._model is not None and self._tokenizer is not None:
            return self._model, self._tokenizer

        with self._lock:
            if self._model is not None and self._tokenizer is not None:
                return self._model, self._tokenizer
            return self._load()

    def _load(self) -> tuple[_BertEmbedding, Tokenizer]:
        cfg = get_settings()
        log.info("downloading %s", cfg.embedding_model)
        model_dir = Path(
            snapshot_download(
                cfg.embedding_model,
                allow_patterns=["*.json", "*.safetensors"],
            )
        )
        with (model_dir / "config.json").open() as f:
            config: dict[str, Any] = json.load(f)

        model = _BertEmbedding(config)

        raw = mx.load(str(model_dir / "model.safetensors"))
        if not isinstance(raw, dict):
            msg = f"Expected dict from mx.load, got {type(raw).__name__}"
            raise TypeError(msg)
        mapped: list[tuple[str, mx.array]] = []
        for key, val in raw.items():
            our_key = _map_hf_key(str(key))
            if our_key is not None and isinstance(val, mx.array):
                mapped.append((our_key, val))
        model.load_weights(mapped)
        mx.eval(model.parameters())

        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=512)
        tokenizer.enable_padding()  # pads to longest in batch, not 512

        # Assign both or neither — avoids partial-load state.
        self._model = model
        self._tokenizer = tokenizer
        log.info("model loaded (dim=%d)", cfg.embedding_dim)
        return model, tokenizer

    # -- sync core (called inside worker thread) -----------------------------

    def _embed_sync(self, texts: list[str]) -> NDArray[np.float32]:
        model, tokenizer = self._ensure_loaded()

        if not texts:
            return np.empty((0, get_settings().embedding_dim), dtype=np.float32)

        log.debug("embedding %d text(s)", len(texts))

        with tracer.start_as_current_span(
            "embed", attributes={"embed.count": len(texts)}
        ):
            parts: list[NDArray[np.float32]] = []
            for i in range(0, len(texts), _BATCH_SIZE):
                chunk = texts[i : i + _BATCH_SIZE]
                encoded = tokenizer.encode_batch(chunk)
                ids = mx.array([e.ids for e in encoded])
                mask = mx.array([e.attention_mask for e in encoded])
                vecs = model(ids, mask)
                vecs = vecs / mx.linalg.norm(vecs, axis=-1, keepdims=True)
                parts.append(np.array(vecs, dtype=np.float32))

            return np.concatenate(parts, axis=0)

    # -- async public API ----------------------------------------------------

    async def embed(self, texts: list[str]) -> NDArray[np.float32]:
        """Return (n, dim) float32 numpy array of L2-normalised embeddings."""
        return await asyncio.to_thread(self._embed_sync, texts)

    async def embed_one(self, text: str) -> NDArray[np.float32]:
        """Return a single (dim,) vector."""
        result = await self.embed([text])
        return result[0]


# Module-level singleton — lazily loaded on first use.
embedder = Embedder()
