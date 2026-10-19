"""The oracle of TEST_PLAN.md 6.1 and the reasons of V3, applied to per-sentence rows.

A row is one sentence of one run: its label and base from the corpus, and
what the cache did (hit, level, the served entry's key, the miss reason).
:func:`annotate` adds ``wrong``, ``critical``, ``flagged``, ``visible``,
``expected`` and, for every servable repeat that missed, ``why``.
"""

from typing import Any

from tts_cache.corpus import DECOY_LABELS, LabelledCorpus

ALLOWED: dict[str, frozenset[str]] = {
    "OFF": frozenset(),
    "A": frozenset({"exact", "surface"}),
    "A+B": frozenset({"exact", "surface", "punct_inner", "punct_end", "filler"}),
    "A+B+C": frozenset({"exact", "surface", "punct_inner", "punct_end", "filler", "paraphrase"}),
}
ALLOWED["A+B+C+J"] = ALLOWED["A+B+C"]  # Level C with its judge: the same serves are allowed
CRITICAL = frozenset({"new", "slot_change", "meaning_change"})
# The level whose index must hold the earlier copy for the label to hit.
LEVEL_FOR = {
    "exact": "A",
    "surface": "A",
    "punct_inner": "B",
    "punct_end": "B",
    "filler": "B",
    "paraphrase": "C",
}
T_LANG = 0.90
# Named causes outside V3's closed list: reported as such, and they fail V3.
OUTSIDE_LIST = frozenset({"deadline", "b_index_miss"})
REASONS = frozenset(
    {
        "same_turn",
        "same_session",
        "rejected",
        "evicted",
        "outside_warm_set",
        "below_t_lang",
        "b_other",
        "slot_veto",
        "provider_failed",
    }
)


def _lang_scope(cache: Any) -> Any:
    return cache._scope


def _inner(matcher: Any) -> Any:
    return getattr(matcher, "inner", matcher)


class Explainer:
    """The ``explain`` callback of ``bench.run_core``: gathers the facts V3 needs on every miss.

    It remembers the key each base's ``new`` sentence stored under, and on a
    miss of a servable label records whether that entry is still in the
    shared store and the warm set, and what Levels B and C made of it.
    """

    def __init__(self, corpus: LabelledCorpus, config: str) -> None:
        self.items = corpus.items
        self.lang = corpus.language
        self.allowed = ALLOWED.get(config, frozenset())
        self.base_key: dict[str, str] = {}
        self.base_canon: dict[str, str] = {}
        self.base_text: dict[str, str] = {}

    async def __call__(self, cache: Any, text: str, sample: Any) -> dict[str, Any] | None:
        from tts_cache.canonical import canonicalise
        from tts_cache.scope import scope_hash
        from tts_cache.stores.tiered import TieredStore

        it = self.items[sample.index]
        if it.label == "new":
            # A probe that failed open returns no key, but the capture still stores under the
            # sentence's own key, so the explanation needs it all the same.
            key = sample.key
            if not key:
                from tts_cache.key import build_key

                key = build_key(_lang_scope(cache), canonicalise(text, self.lang))
            self.base_key[it.base] = key
            self.base_canon[it.base] = canonicalise(text, self.lang)
            self.base_text[it.base] = text
            return None
        if it.label not in self.allowed:
            return None
        target = self.base_key.get(it.base)
        if target is None:
            return {"target": None}
        facts: dict[str, Any] = {"target": target}
        store = cache.store
        scope = _lang_scope(cache)
        if isinstance(store, TieredStore):
            try:
                facts["present"] = (await store.shared.get(target)) is not None
            except Exception as e:  # the fault runs probe a broken store
                facts["present_error"] = type(e).__name__
            warm = store.memory.warm_texts.get(scope_hash(scope), {})
            facts["warm"] = target in warm.values()
        canon = canonicalise(text, self.lang)
        tcanon = self.base_canon.get(it.base, "")
        matcher = _inner(cache.matcher)
        near = getattr(matcher, "near", None)
        if near is not None:
            from tts_cache.match.near import classify

            got = near.probe(scope, canon, text)
            facts["b_nearest"] = None if got is None else {"key": got.key, "class": got.diff_class}
            facts["b_class"] = classify(
                canon, tcanon, self.lang, a_text=text, b_text=self.base_text.get(it.base)
            )
        semantic = getattr(matcher, "semantic", None)
        if semantic is not None and semantic.model_for(scope.language) is not None:
            from tts_cache.match.slots import slots, span_veto

            vecs = semantic._encode(scope.language, [canon, tcanon])
            facts["c_sim"] = round(float(vecs[0] @ vecs[1]), 4)
            facts["c_veto"] = bool(
                slots(tcanon, scope.language, self.base_text.get(it.base))
                != slots(canon, scope.language, text)
                or span_veto(tcanon, canon, scope.language)
            )
            from tts_cache.match.lexicon import lang_of

            idx = semantic._indexes.get((lang_of(scope.language), scope_hash(scope)))
            if idx is not None and idx.items:
                labels, dist = idx.index.knn_query(vecs[:1], k=1)
                item = idx.items[int(labels[0][0])]
                facts["c_nearest"] = {
                    "key": item.key,
                    "sim": round(1.0 - float(dist[0][0]), 4),
                    "veto": bool(
                        item.slots != slots(canon, scope.language, text)
                        or span_veto(item.canonical, canon, scope.language)
                    ),
                }
        return facts


def annotate(
    corpus: LabelledCorpus, rows: list[dict[str, Any]], config: str, reject_keys: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Apply the oracle to one run's rows, in corpus order; returns the same rows, annotated."""
    allowed = ALLOWED[config]
    reject_keys = reject_keys or {}
    items = corpus.items
    source: dict[str, int] = {}  # key -> index of the first sentence that missed and stored under it
    first_new: dict[str, int] = {}
    for it in items:
        if it.label == "new":
            first_new.setdefault(it.base, it.index)
    for row, it in zip(rows, items, strict=True):
        row.setdefault("label", it.label)
        row.setdefault("base", it.base)
        hit = bool(row.get("hit"))
        level_needed = LEVEL_FOR.get(it.label)
        # Visibility: Level A needs a stored copy from an earlier turn, B and C one from an earlier session.
        if level_needed == "A":
            visible = any(
                rows[j].get("key") == row.get("key") and items[j].turn < it.turn and not rows[j].get("hit")
                for j in range(it.index)
            )
        elif level_needed in ("B", "C"):
            j = first_new.get(it.base)
            visible = j is not None and items[j].session < it.session
        else:
            visible = False
        row["visible"] = visible
        wrong = False
        if hit:
            src = source.get(row.get("served_key") or "")
            src_item = items[src] if src is not None else None
            wrong = (
                it.label not in allowed
                or src_item is None
                or src_item.base != it.base
                or src_item.label in DECOY_LABELS
            )
            row["served_from"] = (
                None if src_item is None else {"label": src_item.label, "base": src_item.base}
            )
        elif row.get("key"):
            source.setdefault(row["key"], it.index)
        row["wrong"] = wrong
        row["critical"] = hit and it.label in CRITICAL
        row["flagged"] = hit and it.label == "punct_end"
        facts = row.get("explain") or {}
        expected = it.label in allowed and visible
        if expected and it.label == "paraphrase" and not hit:
            expected = facts.get("c_sim", 0.0) >= T_LANG
        row["expected"] = expected
        if it.label in allowed and not hit:
            row["why"] = why(row, it.label, visible, facts, reject_keys)
    return rows


def why(
    row: dict[str, Any], label: str, visible: bool, facts: dict[str, Any], rejects: dict[str, str]
) -> str:
    """The V3 reason for a servable repeat that missed; ``unexplained:...`` when none applies."""
    if row.get("provider_failed"):
        return "provider_failed"
    level = LEVEL_FOR[label]
    if not visible:
        return "same_turn" if level == "A" else "same_session"
    # The lookup gave up at the store's 20 ms deadline and failed open (a reason outside V3's list).
    if row.get("reason") == "deadline":
        return "deadline"
    target = facts.get("target")
    if target and target in rejects:
        return "rejected"
    if facts.get("present") is False:
        return "evicted"
    if level == "A":
        return "unexplained:" + _brief(facts, row)
    if facts.get("warm") is False:
        return "outside_warm_set"
    if level == "C" or label == "paraphrase":
        sim = facts.get("c_sim")
        if sim is not None and sim < T_LANG:
            return "below_t_lang"
        if facts.get("c_veto"):
            return "slot_veto"
        cn = facts.get("c_nearest") or {}
        if cn.get("key") and cn["key"] != target and cn.get("veto"):
            return "slot_veto:shadowed"
        return "unexplained:" + _brief(facts, row)
    # What the cache's own Level B probe saw first: its nearest entry may be another variant (shadowed).
    b_class = row.get("near_class") or facts.get("b_class")
    nearest = (facts.get("b_nearest") or {}).get("key")
    if b_class in ("other", "slot") and nearest and nearest != target:
        return ("b_other" if b_class == "other" else "slot_veto") + ":shadowed"
    if b_class == "other":
        return "b_other"
    if b_class == "slot":
        return "slot_veto"
    # The near-duplicate index (MinHash bands) returned no candidate for a pair the classifier
    # would serve: an index miss, a reason outside V3's list.
    if b_class in ("punctuation", "filler") and facts.get("b_nearest", "absent") is None:
        return "b_index_miss"
    return "unexplained:" + _brief(facts, row)


def _brief(facts: dict[str, Any], row: dict[str, Any]) -> str:
    keep = {k: facts.get(k) for k in ("b_class", "b_nearest", "c_sim", "c_veto", "c_nearest") if k in facts}
    if row.get("reason"):
        keep["miss_reason"] = row["reason"]
    if row.get("near_class"):
        keep["near_class"] = row["near_class"]
    return ",".join(f"{k}={v}" for k, v in keep.items()) or "no_facts"


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The cost and correctness figures of one run (7.1, 7.2)."""
    chars = sum(r["chars"] for r in rows)
    hits = [r for r in rows if r.get("hit")]
    served = sum(r["chars"] for r in hits if not r["wrong"])
    servable = sum(r["chars"] for r in rows if r["label"] in LEVEL_FOR)
    expected = sum(r["chars"] for r in rows if r.get("expected"))
    served_expected = sum(r["chars"] for r in hits if r.get("expected") and not r["wrong"])
    by_level: dict[str, int] = {}
    for r in hits:
        k = r.get("level") or "?"
        if k == "B" and r.get("diff_class"):
            k = f"B:{r['diff_class']}"
        by_level[k] = by_level.get(k, 0) + 1
    whys: dict[str, int] = {}
    for r in rows:
        if "why" in r:
            k = r["why"].split(":", 1)[0]
            whys[k] = whys.get(k, 0) + 1
    return {
        "sentences": len(rows),
        "chars": chars,
        "hits": len(hits),
        "hit_chars": sum(r["chars"] for r in hits),
        "served_chars": served,
        "char_hit_rate": served / chars if chars else 0.0,
        "sentence_hit_rate": sum(1 for r in hits if not r["wrong"]) / len(rows) if rows else 0.0,
        "capture_eff_servable": served / servable if servable else None,
        "capture_eff_expected": served_expected / expected if expected else None,
        "wrong": sum(1 for r in hits if r["wrong"]),
        "wrong_by_label": _count(r["label"] for r in hits if r["wrong"]),
        "critical": sum(1 for r in rows if r["critical"]),
        "flagged": sum(1 for r in rows if r["flagged"]),
        "by_level": by_level,
        "why": whys,
        "unexplained": sum(1 for r in rows if r.get("why", "").startswith("unexplained")),
        "outside_list": sum(1 for r in rows if r.get("why", "").split(":", 1)[0] in OUTSIDE_LIST),
        "shadowed": sum(1 for r in rows if r.get("why", "").endswith(":shadowed")),
    }


def _count(xs: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out
