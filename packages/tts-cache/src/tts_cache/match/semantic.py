"""Level C: semantic paraphrase (SOLUTION.md 5 and 6, ``[similarity]`` extra).

A sentence encoder per language through a routing map, an in-process HNSW
cosine index per ``(language, scope)`` over the hot set, a per-entry
acceptance threshold that starts at a global default, and two vetoes from
:mod:`tts_cache.match.slots`: the nearest entry is served only when it carries
exactly the same numbers, amounts, dates, names and foreign-script words as
the query (the slot veto), and only when the two texts differ in more than one
short contiguous span (the span veto; one changed span is a changed fact, not
a paraphrase). The encoder proposes, the vetoes dispose. The default encoder
is ``multilingual-e5-small`` run through onnxruntime, about 2.5 ms per
sentence on one CPU thread; the model2vec static model stays available as a
route for languages where speed matters more than recall. A Level C hit plays
the stored clip, so ``Hit.spoken_text`` is the stored text and every host must
write that text to its transcript and context.

A matcher built with a ``judge`` (:mod:`tts_cache.match.judge`, opt-in) asks
it about the candidate the vetoes let through and serves only when the judge
finds that each sentence entails the other; the check runs in a worker
thread, off the event loop.

A language serves Level C only after it passes the acceptance check of
section 5 (:func:`acceptance`): 200 pairs, half paraphrases and half
near-misses with one changed slot; at the chosen threshold no near-miss may be
accepted and at least 80 percent of paraphrases must be. ``ROUTES`` records
the outcome: a language that failed, or has not been checked, maps to None.
"""

import asyncio
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from tts_cache.match import Match
from tts_cache.match.judge import JUDGE_DEADLINE_MS, JUDGE_THRESHOLD, Judge
from tts_cache.match.lexicon import lang_of
from tts_cache.match.slots import slots, span_veto
from tts_cache.policy import Level
from tts_cache.scope import Scope, scope_hash

DEFAULT_MODEL = "intfloat/multilingual-e5-small"
STATIC_MODEL = "minishlab/potion-multilingual-128M"
# Languages both models were trained on (section 6); each is a candidate for
# Level C until its acceptance check passes.
CANDIDATES = ("en", "hi", "bn", "ta", "te", "kn", "ml", "mr", "gu", "pa", "or", "as")
# The acceptance check of 2026-10-18 with both vetoes (tests/test_semantic.py,
# README "Status"): in all twelve languages the vetoes stop 100 of 100
# near-misses and multilingual-e5-small accepts 85 to 100 percent of
# paraphrases at the 0.90 floor, so every candidate routes to it. The static
# model reaches 57, 21 and 21 percent in en, hi and ta and is not routed.
ROUTES: dict[str, str | None] = {lang: DEFAULT_MODEL for lang in CANDIDATES}
# The lowest threshold the acceptance check may choose; the veto, not the
# threshold, is what keeps changed slots out, so this guards the cases the
# fixtures do not cover (a changed fact that no detector recognises).
FLOOR_THRESHOLD = 0.90
DEFAULT_THRESHOLD = FLOOR_THRESHOLD
PASS_PARAPHRASE_SHARE = 0.8


class Encoder(Protocol):
    def encode(self, sentences: Sequence[str]) -> Any: ...


_MODELS: dict[str, Any] = {}
_MODELS_LOCK = threading.Lock()


class OnnxEncoder:
    """A sentence-transformer exported to ONNX, run with onnxruntime on one CPU thread.

    Loads ``onnx/model.onnx`` and ``onnx/tokenizer.json`` from the Hugging Face
    hub (cached locally after the first download), mean-pools the last hidden
    state over the attention mask and L2-normalises. ``multilingual-e5-small``
    measures 2.3 ms p50 and 3.1 ms p99 per sentence on an M4 Pro.
    """

    def __init__(self, name: str, *, threads: int = 1, max_tokens: int = 128) -> None:
        import onnxruntime as rt
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        self.name = name
        self._tok = Tokenizer.from_file(hf_hub_download(name, "onnx/tokenizer.json"))
        self._tok.enable_truncation(max_tokens)
        opts = rt.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self._sess = rt.InferenceSession(
            hf_hub_download(name, "onnx/model.onnx"), opts, providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._sess.get_inputs()}

    def encode(self, sentences: Sequence[str]) -> Any:
        import numpy as np

        out = []
        for s in sentences:
            enc = self._tok.encode(s)
            ids = np.asarray([enc.ids], dtype=np.int64)
            mask = np.asarray([enc.attention_mask], dtype=np.int64)
            feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = np.asarray(self._sess.run(None, feed)[0], dtype=np.float32)
            weights = mask[..., None].astype(np.float32)
            vec = (hidden * weights).sum(axis=1) / weights.sum(axis=1)
            out.append(vec[0])
        return np.stack(out)


def load_model(name: str = DEFAULT_MODEL, *, float16: bool = True) -> Any:
    """The encoder, loaded once per process.

    A model2vec name (``minishlab/...``) loads a static model with float16
    embeddings; any other name loads :class:`OnnxEncoder`.
    """
    with _MODELS_LOCK:
        model = _MODELS.get(name)
        if model is None:
            if name.startswith("minishlab/"):
                import numpy as np
                from model2vec import StaticModel

                model = StaticModel.from_pretrained(name)
                if float16:
                    model.embedding = model.embedding.astype(np.float16)
            else:
                model = OnnxEncoder(name)
            _MODELS[name] = model
        return model


def _unit(vectors: Any) -> Any:
    import numpy as np

    v = np.asarray(vectors, dtype=np.float32)
    if v.ndim == 1:
        v = v[None, :]
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)


@dataclass
class _Item:
    key: str
    canonical: str
    text: str
    threshold: float
    slots: frozenset[str] = frozenset()


class _Index:
    def __init__(self, dim: int) -> None:
        import hnswlib

        self.index = hnswlib.Index(space="cosine", dim=dim)
        self.capacity = 1024
        self.index.init_index(max_elements=self.capacity, ef_construction=100, M=16)
        self.index.set_ef(64)
        self.items: list[_Item] = []
        self.keys: dict[str, int] = {}


class SemanticMatcher:
    """Level C over an HNSW index per ``(language, scope)``.

    ``routes`` maps a language to a model name or None (Level C off for it);
    ``encoder`` replaces the model for every language, for tests. Thresholds
    are cosine similarities; an entry's own threshold, when the store has
    learned one, overrides ``threshold``. ``judge`` scores the candidate that
    clears the threshold and the vetoes, and it is served only when the score
    reaches ``judge_threshold``; ``judged`` counts the candidates it refused.
    """

    def __init__(
        self,
        routes: Mapping[str, str | None] | None = None,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        encoder: Encoder | None = None,
        judge: Judge | None = None,
        judge_threshold: float = JUDGE_THRESHOLD,
    ) -> None:
        self.routes = dict(ROUTES if routes is None else routes)
        self.threshold = threshold
        self._encoder = encoder
        self.judge = judge
        self.judge_threshold = judge_threshold
        self.judged = 0
        self._indexes: dict[tuple[str, str], _Index] = {}
        self._lock = threading.Lock()
        self.vetoed = 0

    @property
    def extra_deadline_ms(self) -> float:
        """What this matcher adds to a cache's default lookup deadline: the judge's budget, if it has one."""
        return JUDGE_DEADLINE_MS if self.judge is not None else 0.0

    def model_for(self, language: str) -> str | None:
        return self.routes.get(lang_of(language))

    def _encode(self, language: str, texts: Sequence[str]) -> Any:
        if self._encoder is not None:
            return _unit(self._encoder.encode(list(texts)))
        name = self.model_for(language)
        assert name is not None
        return _unit(load_model(name).encode(list(texts)))

    def add(
        self, scope: Scope, key: str, canonical: str, text: str | None = None, threshold: float | None = None
    ) -> None:
        if self.model_for(scope.language) is None:
            return
        vec = self._encode(scope.language, [canonical])
        ident = (lang_of(scope.language), scope_hash(scope))
        with self._lock:
            idx = self._indexes.get(ident)
            if idx is None:
                idx = self._indexes[ident] = _Index(vec.shape[1])
            if key in idx.keys:
                return
            if len(idx.items) >= idx.capacity:
                idx.capacity *= 2
                idx.index.resize_index(idx.capacity)
            label = len(idx.items)
            idx.index.add_items(vec, [label])
            idx.items.append(
                _Item(
                    key,
                    canonical,
                    text or canonical,
                    threshold or self.threshold,
                    slots(canonical, scope.language, text),
                )
            )
            idx.keys[key] = label

    def refresh(self, scope: Scope, memory: Any) -> int:
        """Index the hot-set texts held in ``memory`` (a ``MemoryStore``); returns the entries indexed."""
        if self.model_for(scope.language) is None:
            return 0
        n = 0
        for canonical, key in dict(memory.warm_texts.get(scope_hash(scope), {})).items():
            entry = memory.peek(key)
            self.add(scope, key, canonical, entry.text if entry is not None else None)
            n += 1
        return n

    def probe(self, scope: Scope, canonical: str, text: str | None = None) -> tuple[_Item, float] | None:
        """The nearest indexed entry and its similarity, if it clears the entry's threshold and the slot veto.

        ``vetoed`` on the matcher counts candidates that cleared the threshold
        but carried different slots; the cache reports them as ``near`` events
        with class ``slot`` so the histogram shows what the veto stopped.
        """
        if self.model_for(scope.language) is None:
            return None
        idx = self._indexes.get((lang_of(scope.language), scope_hash(scope)))
        if idx is None or not idx.items:
            return None
        vec = self._encode(scope.language, [canonical])
        labels, distances = idx.index.knn_query(vec, k=1)
        item = idx.items[int(labels[0][0])]
        similarity = 1.0 - float(distances[0][0])
        if item.canonical == canonical or similarity < item.threshold:
            return None
        if item.slots != slots(canonical, scope.language, text) or span_veto(
            item.canonical, canonical, scope.language
        ):
            self.vetoed += 1
            return None
        return item, similarity

    async def find(
        self,
        scope: Scope,
        canonical: str,
        levels: set[Level],
        *,
        namespace: str | None = None,
        text: str | None = None,
    ) -> Match | None:
        if "C" not in levels or namespace is not None:
            return None
        found = self.probe(scope, canonical, text)
        if found is None:
            return None
        item, similarity = found
        if self.judge is not None:
            score = await asyncio.to_thread(self.judge.score, item.text, text or canonical)
            if score < self.judge_threshold:
                self.judged += 1
                return None
        return Match(item.key, "C", item.canonical, score=similarity)


@dataclass(frozen=True)
class Acceptance:
    language: str
    pairs: int
    threshold: float
    paraphrase_share: float
    near_miss_accepted: int
    near_miss_max: float
    paraphrase_mean: float
    near_miss_mean: float
    near_miss_vetoed: int = 0
    paraphrase_vetoed: int = 0

    @property
    def passed(self) -> bool:
        return self.near_miss_accepted == 0 and self.paraphrase_share >= PASS_PARAPHRASE_SHARE


def acceptance(
    language: str, pairs: Iterable[Mapping[str, str]], encoder: Encoder, *, veto: bool = True
) -> Acceptance:
    """The section 5 check: the threshold that accepts no near-miss, and the paraphrases it accepts.

    A pair is accepted when the slot veto lets it through and its similarity
    reaches the threshold. The threshold is the lowest, not below
    ``FLOOR_THRESHOLD``, that rejects every near-miss the veto did not stop;
    the language passes if the accepted paraphrase share reaches 80 percent.
    ``veto=False`` runs the check on the embedding alone.
    """
    from tts_cache.canonical import canonicalise

    rows = list(pairs)
    ca = [canonicalise(p["a"], language) for p in rows]
    cb = [canonicalise(p["b"], language) for p in rows]
    a = _unit(encoder.encode(ca))
    b = _unit(encoder.encode(cb))
    sims = (a * b).sum(axis=1)
    passed_veto = [
        (not veto)
        or (slots(x, language, p["a"]) == slots(y, language, p["b"]) and not span_veto(x, y, language))
        for x, y, p in zip(ca, cb, rows, strict=True)
    ]
    scored = list(zip(sims, passed_veto, rows, strict=True))
    para = [(float(s), v) for s, v, p in scored if p["label"] == "paraphrase"]
    near = [(float(s), v) for s, v, p in scored if p["label"] == "near_miss"]
    unvetoed_near = [s for s, v in near if v]
    threshold = max(FLOOR_THRESHOLD if veto else 0.0, (max(unvetoed_near) + 1e-6) if unvetoed_near else 0.0)
    return Acceptance(
        language=language,
        pairs=len(rows),
        threshold=round(threshold, 6),
        paraphrase_share=sum(v and s >= threshold for s, v in para) / len(para),
        near_miss_accepted=sum(v and s >= threshold for s, v in near),
        near_miss_max=round(max(s for s, _ in near), 6),
        paraphrase_mean=round(sum(s for s, _ in para) / len(para), 4),
        near_miss_mean=round(sum(s for s, _ in near) / len(near), 4),
        near_miss_vetoed=sum(not v for _, v in near),
        paraphrase_vetoed=sum(not v for _, v in para),
    )
