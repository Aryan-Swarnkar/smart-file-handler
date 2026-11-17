"""ML model registry: models load ONCE per worker process.

`get_registry()` returns the process-wide registry chosen by ML_STUB:

- `StubRegistry` (default): fixed-delay fakes, deterministic per file_id —
  same file always labels the same way, so tests are reproducible and a
  duplicate delivery produces an identical replacement set (idempotent).
- `ClipRegistry` (ML_STUB=0): real open_clip zero-shot. torch/open_clip are
  an OPTIONAL uv dependency-group (`ml`) that never goes into the container
  image — the import is deferred into the methods, so importing this module
  (and running the whole stub stack) needs no torch.

Both registries answer the same two questions:
- label_image(path) -> [(name, confidence)] from a fixed candidate list,
  filtered by LABEL_CONFIDENCE_THRESHOLD;
- topic_document(text) -> one of the six doc topics (work/study/finance/
  id/receipt/other).

The candidate/concept lists are code constants (spec vocabulary), not env
knobs — they change with a code review, not with a config push.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# CLIP zero-shot candidate concepts (image pipeline vocabulary). ~16 covers
# the spec's smart categories without an open-ended label space.
IMAGE_CONCEPTS: tuple[str, ...] = (
    "animal", "dog", "cat", "car", "food", "drink", "building", "nature",
    "people", "document", "screenshot", "chart", "map", "art", "music",
    "sports", "travel",
)

# Document topics (pdf pipeline vocabulary) — the spec's fixed six.
DOC_TOPICS: tuple[str, ...] = (
    "work", "study", "finance", "id", "receipt", "other",
)

# The deterministic stub sleeps this long per call (visible pipeline work,
# cheap enough for tests).
STUB_DELAY_SECONDS = 0.03


class ImageLabels(NamedTuple):
    labels: list[tuple[str, float]]  # (concept, confidence 0..1)


class DocumentTopic(NamedTuple):
    topic: str
    confidence: float


class Registry(Protocol):
    async def label_image(self, file_id: uuid.UUID, path: Path) -> ImageLabels: ...
    async def topic_document(self, file_id: uuid.UUID, text: str) -> DocumentTopic: ...
    async def warmup(self) -> None: ...


def _seed_from(file_id: uuid.UUID) -> int:
    """Stable per-file seed: the SAME file always gets the SAME stub answer,
    across processes and reprocessing runs (deterministic idempotency)."""
    return int(uuid.UUID(bytes=file_id.bytes).hex[:12], 16)


class StubRegistry:
    """Fixed-delay fake. No model files, no network, no decode: the answers
    depend only on the file_id seed (and the text hash for topics)."""

    async def label_image(self, file_id: uuid.UUID, path: Path) -> ImageLabels:
        await asyncio.sleep(STUB_DELAY_SECONDS)
        rng = random.Random(_seed_from(file_id))
        picks = rng.sample(IMAGE_CONCEPTS, k=rng.randint(2, 4))
        labels = [(name, round(rng.uniform(0.75, 0.98), 3)) for name in picks]
        return ImageLabels(labels=labels)

    async def topic_document(self, file_id: uuid.UUID, text: str) -> DocumentTopic:
        await asyncio.sleep(STUB_DELAY_SECONDS)
        rng = random.Random(_seed_from(file_id))
        topic = rng.choice(DOC_TOPICS)
        return DocumentTopic(topic=topic, confidence=round(rng.uniform(0.6, 0.95), 3))

    async def warmup(self) -> None:
        return None  # nothing to pre-load


class ClipRegistry:
    """Real open_clip zero-shot. The model loads once per process at
    `warmup()` (wired to celery's worker_process_init); if a call arrives
    before warmup completed, the first call loads it lazily instead.

    open_clip/torch are the optional `ml` dependency group — importing them
    happens HERE, lazily, never at module import (the container image has no
    torch; the manual real-model run installs the group on the host)."""

    _model: Any = None
    _preprocess: Any = None
    _concept_features: Any = None  # normalized text embeddings
    _lock: asyncio.Lock | None = None

    # TODO(verify-at-gated-run): open_clip API + model variant are verified
    # against the INSTALLED versions during the explicit real-model checkpoint
    # run (ML_STUB convention: never run alongside the full stack). The stub
    # path below never executes this class.

    async def _ensure_loaded(self) -> tuple[Any, Any]:
        if self._model is not None:
            return self._model, self._preprocess
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._model is not None:
                return self._model, self._preprocess
            import open_clip  # deferred: optional `ml` dependency group

            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32", pretrained="openai"
            )
            tokenizer = open_clip.get_tokenizer("ViT-B-32")
            import torch  # deferred with open_clip

            device = "cpu"
            model = model.to(device).eval()
            prompts = [f"a photo of {c}" for c in IMAGE_CONCEPTS]
            with torch.no_grad():
                tokens = tokenizer(prompts).to(device)
                text_features = model.encode_text(tokens)
                text_features = text_features / text_features.norm(
                    dim=-1, keepdim=True
                )
            self._model = model
            self._preprocess = preprocess
            self._concept_features = (text_features, device)
            logger.info("clip_model_loaded", variant="ViT-B-32", device=device)
        return self._model, self._preprocess

    async def label_image(self, file_id: uuid.UUID, path: Path) -> ImageLabels:
        import torch  # deferred: optional `ml` dependency group
        from PIL import Image

        model, preprocess = await self._ensure_loaded()
        assert self._concept_features is not None
        text_features, device = self._concept_features

        with Image.open(path) as img:
            image = img.convert("RGB")
        image_input = preprocess(image).unsqueeze(0).to(device)
        with torch.no_grad():
            image_features = model.encode_image(image_input)
            image_features = image_features / image_features.norm(
                dim=-1, keepdim=True
            )
            probs = (image_features @ text_features.T).softmax(dim=-1)[0]
        pairs = [
            (concept, float(probs[i]))
            for i, concept in enumerate(IMAGE_CONCEPTS)
            if float(probs[i]) >= get_settings().LABEL_CONFIDENCE_THRESHOLD
        ]
        pairs.sort(key=lambda p: p[1], reverse=True)
        return ImageLabels(labels=pairs)

    async def topic_document(self, file_id: uuid.UUID, text: str) -> DocumentTopic:
        import torch  # deferred: optional `ml` dependency group

        model, _ = await self._ensure_loaded()
        import open_clip  # deferred

        tokenizer = open_clip.get_tokenizer("ViT-B-32")
        assert self._concept_features is not None
        _, device = self._concept_features
        sample = " ".join(text.split())[:512] or "empty document"
        prompts = [
            f"a document about {topic}" if topic != "other" else "a document"
            for topic in DOC_TOPICS
        ]
        with torch.no_grad():
            tokens = tokenizer(prompts).to(device)
            text_features = model.encode_text(tokens)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            doc_features = model.encode_text(tokenizer([sample]).to(device))
            doc_features = doc_features / doc_features.norm(dim=-1, keepdim=True)
            probs = (doc_features @ text_features.T).softmax(dim=-1)[0]
        best = max(range(len(DOC_TOPICS)), key=lambda i: float(probs[i]))
        return DocumentTopic(topic=DOC_TOPICS[best], confidence=float(probs[best]))

    async def warmup(self) -> None:
        await self._ensure_loaded()


_REGISTRY: Registry | None = None


def get_registry() -> Registry:
    """Process-wide registry singleton. Built once per worker process; the
    forked pool children see the stub (cheap) or trigger lazy CLIP loads."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ClipRegistry() if not get_settings().ML_STUB else StubRegistry()
    return _REGISTRY


async def warmup() -> None:
    """worker_process_init hook: pre-load real models once per process.
    Stub: a no-op (logged once so operators see which mode booted)."""
    registry = get_registry()
    if isinstance(registry, StubRegistry):
        logger.info("ml_registry_warmed", mode="stub")
        return
    logger.info("ml_registry_warming", mode="clip")
    await registry.warmup()
