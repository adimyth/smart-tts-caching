"""The replay harness: an application's log through the production splitter, canonicaliser and policy (9.2).

The replay simulates the cache's decisions in time order without audio: a
sentence hits when an unexpired entry holds its key; a miss is stored when the
policy says so, with the policy's TTL; a hit refreshes the TTL as the runtime
does. A captured clip is stored only once the provider has returned all of
its audio, so a stored entry becomes visible ``capture_delay_s`` after the
sentence was spoken (2 s by default): a sentence repeated later in the same
turn still misses, as it does at runtime. Sizes are estimated from characters (``bytes_per_char``, about 265
bytes of Opus per character for 4 s of speech per 60 characters). The hot set
is ordered by the same forward-decayed hit count the stores use.
"""

import heapq
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tts_cache.canonical import canonicalise, rule_names
from tts_cache.corpus import Event
from tts_cache.policy import DEFAULT_POLICY, Clip, SentenceCtx
from tts_cache.scope import Scope
from tts_cache.split import split_text
from tts_cache.store import HOT_HALF_LIFE_S

BYTES_PER_CHAR = 265
MS_PER_CHAR = 66
WARM_BUDGETS_MB = (2, 4, 8)
GAP_BUCKETS = [
    (60, "<1m"),
    (3600, "<1h"),
    (86400, "<1d"),
    (2 * 86400, "<2d"),
    (7 * 86400, "<7d"),
    (30 * 86400, "<30d"),
]


@dataclass
class _Entry:
    text: str
    created: float
    expires_at: float
    visible_at: float = 0.0
    hits: int = 0
    score: float = 0.0  # forward-decayed, comparable within one replay
    chars: int = 0
    nbytes: int = 0


@dataclass
class ReplayResult:
    sentences: int = 0
    chars: int = 0
    hit_chars: int = 0
    hits: int = 0
    timeline: list[dict[str, Any]] = field(default_factory=list)
    steady_state_hit_rate: float = 0.0
    size_entries: int = 0
    size_bytes: int = 0
    gaps: list[float] = field(default_factory=list)
    warm_hit_chars: dict[int, int] = field(default_factory=dict)
    hot_set: dict[str, list[str]] = field(default_factory=dict)

    @property
    def hit_rate(self) -> float:
        return self.hit_chars / self.chars if self.chars else 0.0


def _policy_scope(label: str, language: str) -> Scope:
    return Scope(service=label, model="", voice="", language=language, sample_rate=0)


def _forward(ts: float, t0: float) -> float:
    return 2.0 ** ((ts - t0) / HOT_HALF_LIFE_S)


def simulate(
    events: Sequence[Event],
    policy: Any = DEFAULT_POLICY,
    *,
    disabled_rules: Iterable[str] = (),
    bytes_per_char: int = BYTES_PER_CHAR,
    warm_budgets_mb: Sequence[int] = WARM_BUDGETS_MB,
    buckets: int = 10,
    hot_set_bytes: int = 4 << 20,
    capture_delay_s: float = 2.0,
) -> ReplayResult:
    disabled = frozenset(disabled_rules)
    out = ReplayResult(warm_hit_chars={mb: 0 for mb in warm_budgets_mb})
    entries: dict[tuple[str, str], _Entry] = {}
    first_seen: dict[tuple[str, str], float] = {}
    second_seen: set[tuple[str, str]] = set()
    warm_sets: dict[int, set[tuple[str, str]]] = {}
    session_of: dict[str, bool] = {}
    t0 = events[0].ts if events else 0.0
    rows: list[tuple[int, int]] = []  # (chars, hit chars) per sentence, in order
    for ev in events:
        if ev.session not in session_of:
            # A new session starts warm: the hot set within each budget, as of now.
            session_of[ev.session] = True
            live = [
                (k, e)
                for k, e in entries.items()
                if k[0] == ev.scope and e.visible_at <= ev.ts < e.expires_at
            ]
            for mb in warm_budgets_mb:
                chosen, used = set(), 0
                for k, e in heapq.nlargest(len(live), live, key=lambda kv: kv[1].score):
                    if used + e.nbytes > mb << 20:
                        break
                    chosen.add(k)
                    used += e.nbytes
                warm_sets[mb] = chosen
        sc = _policy_scope(ev.scope, ev.language)
        for sentence in split_text(ev.text, ev.language):
            canonical = canonicalise(sentence, ev.language, disabled=disabled)
            s = SentenceCtx(sentence, canonical, ev.language, sc, tags=ev.tags or {}, now=ev.ts)
            chars = s.chars
            key = (ev.scope, canonical)
            out.sentences += 1
            out.chars += chars
            if key in first_seen and key not in second_seen:
                second_seen.add(key)
                out.gaps.append(ev.ts - first_seen[key])
            first_seen.setdefault(key, ev.ts)
            e = entries.get(key)
            if policy.should_lookup(s) and e is not None and e.visible_at <= ev.ts < e.expires_at:
                e.hits += 1
                e.score += _forward(ev.ts, t0)
                promoted = _Promoted(e.hits)
                e.expires_at = ev.ts + (policy.ttl_for(s, promoted) or 0)  # type: ignore[arg-type]
                out.hits += 1
                out.hit_chars += chars
                rows.append((chars, chars))
                for mb, ws in warm_sets.items():
                    if key in ws:
                        out.warm_hit_chars[mb] += chars
                continue
            rows.append((chars, 0))
            if e is not None and ev.ts < e.visible_at:
                continue  # its first capture is still in flight
            nbytes = chars * bytes_per_char
            clip = Clip(chars * MS_PER_CHAR, 24000, 1, nbytes)
            if policy.should_store(s, clip):
                ttl = policy.ttl_for(s, None) or 0
                entries[key] = _Entry(
                    sentence,
                    ev.ts,
                    ev.ts + ttl,
                    ev.ts + capture_delay_s,
                    0,
                    _forward(ev.ts, t0),
                    chars,
                    nbytes,
                )
    # Hit rate over time and at steady state (the second half of the sentences).
    if rows:
        per = max(1, math.ceil(len(rows) / buckets))
        for b in range(0, len(rows), per):
            chunk = rows[b : b + per]
            c = sum(r[0] for r in chunk)
            out.timeline.append(
                {"from": b, "to": b + len(chunk), "hit_rate": sum(r[1] for r in chunk) / c if c else 0.0}
            )
        tail = rows[len(rows) // 2 :]
        c = sum(r[0] for r in tail)
        out.steady_state_hit_rate = sum(r[1] for r in tail) / c if c else 0.0
    end = events[-1].ts if events else 0.0
    live = {k: e for k, e in entries.items() if e.expires_at > end}
    out.size_entries = len(live)
    out.size_bytes = sum(e.nbytes for e in live.values())
    by_scope: dict[str, list[_Entry]] = defaultdict(list)
    for (label, _), e in live.items():
        by_scope[label].append(e)
    for label, es in by_scope.items():
        es.sort(key=lambda e: e.score, reverse=True)
        chosen, used = [], 0
        for e in es:
            if used + e.nbytes > hot_set_bytes:
                break
            chosen.append(e.text)
            used += e.nbytes
        out.hot_set[label] = chosen
    return out


@dataclass(frozen=True)
class _Promoted:
    """What the policy sees of an entry being hit: its hit count after this hit."""

    hit_count: int


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def report(
    events: Sequence[Event], policy: Any = DEFAULT_POLICY, *, rules: bool = True, **kw: Any
) -> dict[str, Any]:
    """The full replay report of section 9.2 as a JSON-ready dict."""
    full = simulate(events, policy, **kw)
    increments: dict[str, float] = {}
    if rules:
        for name in rule_names():
            without = simulate(events, policy, disabled_rules={name}, **kw)
            increments[name] = round(full.hit_rate - without.hit_rate, 4)
    histogram = {label: 0 for _, label in GAP_BUCKETS} | {">=30d": 0}
    for g in full.gaps:
        for limit, label in GAP_BUCKETS:
            if g < limit:
                histogram[label] += 1
                break
        else:
            histogram[">=30d"] += 1
    return {
        "events": len(events),
        "sentences": full.sentences,
        "chars": full.chars,
        "hits": full.hits,
        "hit_rate": round(full.hit_rate, 4),
        "steady_state_hit_rate": round(full.steady_state_hit_rate, 4),
        "timeline": [{**t, "hit_rate": round(t["hit_rate"], 4)} for t in full.timeline],
        "rule_increments": increments,
        "size": {"entries": full.size_entries, "bytes": full.size_bytes},
        "gap_first_to_second_s": {
            "count": len(full.gaps),
            "p50": round(_percentile(full.gaps, 0.5), 1),
            "p90": round(_percentile(full.gaps, 0.9), 1),
            "p99": round(_percentile(full.gaps, 0.99), 1),
            "histogram": histogram,
        },
        "warm_coverage": {
            f"{mb}MB": round(full.warm_hit_chars[mb] / full.hit_chars, 4) if full.hit_chars else 0.0
            for mb in full.warm_hit_chars
        },
        "hot_set": full.hot_set,
    }


def format_report(r: dict[str, Any]) -> str:
    lines = [
        f"events {r['events']}, sentences {r['sentences']}, characters {r['chars']}",
        f"hit rate (characters) {r['hit_rate']:.1%}, steady state {r['steady_state_hit_rate']:.1%}, "
        f"hits {r['hits']}",
        "hit rate over time: " + " ".join(f"{t['hit_rate']:.0%}" for t in r["timeline"]),
        f"steady-state size: {r['size']['entries']} entries, {r['size']['bytes'] / 1e6:.2f} MB",
        "warm-set share of hits: " + ", ".join(f"{k} {v:.1%}" for k, v in r["warm_coverage"].items()),
    ]
    g = r["gap_first_to_second_s"]
    pct = f"p50={g['p50']}s p90={g['p90']}s p99={g['p99']}s"
    lines.append(f"first-to-second gap: n={g['count']} {pct} {g['histogram']}")
    if r["rule_increments"]:
        lines.append("incremental hit rate per canonicalisation rule:")
        for name, delta in sorted(r["rule_increments"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name:16} {delta:+.2%}")
    return "\n".join(lines)
