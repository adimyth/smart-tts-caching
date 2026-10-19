"""The synthetic benchmark corpus and the replay log format (SOLUTION.md 9.2, 9.3).

Replay log format, one JSON object per line, in any order (replay sorts by ``ts``):

    {"ts": 1792300000.0, "session": "s-0001", "text": "Hello. How can I help?", "language": "en-US",
     "scope": "sarvam/bulbul:v3/shubh", "tags": {"tenant": "acme"}}

- ``ts``: seconds since the epoch when the text was spoken;
- ``session``: any string identifying the conversation;
- ``text``: what the agent sent to the provider, one sentence or a whole turn
  (replay splits it with the production splitter);
- ``language``: the BCP 47 code the canonicaliser uses;
- ``scope``: an opaque label for the voice identity; entries share audio only
  within one label;
- ``tags`` (optional): string pairs, for policies that route on them.
"""

import json
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Word lists for synthetic sentences. No word carries a full stop, so the
# splitter ends every generated sentence exactly where the generator does.
VOCAB: dict[str, list[str]] = {  # pyright: ignore[reportAssignmentType]
    "en": (
        "account order refund payment delivery address plan support team today tomorrow morning "
        "please thank you sure check confirm update help minute moment message phone email card "
        "balance ticket number store hour week month service request status issue problem detail "
        "time date offer price bill charge renewal subscription device network signal app website "
        "password login profile record history agent specialist manager reply call back soon now "
        "already again still also only just really quickly safely easily right away for with from "
        "your our this that the a an is are was will can could would should have has been we you "
        "it they here there about into over under after before while because so and but or"
    ).split(),
    "hi": (
        "आपका खाता ऑर्डर रिफंड भुगतान डिलीवरी पता प्लान सहायता टीम आज कल सुबह कृपया धन्यवाद ज़रूर "
        "जाँच पुष्टि अपडेट मदद मिनट संदेश फ़ोन ईमेल कार्ड बैलेंस टिकट नंबर स्टोर घंटा हफ़्ता महीना सेवा "
        "अनुरोध स्थिति समस्या जानकारी समय तारीख़ ऑफ़र कीमत बिल शुल्क नवीनीकरण डिवाइस नेटवर्क ऐप "
        "वेबसाइट पासवर्ड प्रोफ़ाइल रिकॉर्ड एजेंट विशेषज्ञ प्रबंधक जवाब जल्दी अभी पहले फिर भी केवल "
        "सच में तुरंत आसानी से के लिए साथ से में पर है हैं था होगा सकते सकती हम आप यह वह यहाँ वहाँ"
    ).split(),
    "ta": (
        "உங்கள் கணக்கு ஆர்டர் பணத்திருப்பம் கட்டணம் விநியோகம் முகவரி திட்டம் உதவி குழு இன்று நாளை "
        "காலை தயவுசெய்து நன்றி சரி சரிபார்ப்பு உறுதி புதுப்பிப்பு நிமிடம் செய்தி தொலைபேசி மின்னஞ்சல் "
        "அட்டை இருப்பு டிக்கெட் எண் கடை மணி வாரம் மாதம் சேவை கோரிக்கை நிலை சிக்கல் விவரம் நேரம் "
        "தேதி சலுகை விலை பில் புதுப்பித்தல் சாதனம் நெட்வொர்க் செயலி இணையதளம் கடவுச்சொல் சுயவிவரம் "
        "பதிவு முகவர் நிபுணர் மேலாளர் பதில் விரைவில் இப்போது ஏற்கனவே மீண்டும் மட்டும் உடனே எளிதாக "
        "க்காக உடன் இருந்து இல் மேல் உள்ளது இருக்கும் முடியும் நாங்கள் நீங்கள் இது அது இங்கே அங்கே"
    ).split(),
}


@dataclass(frozen=True)
class Event:
    ts: float
    session: str
    text: str
    language: str
    scope: str
    tags: dict[str, str] | None = None

    def to_json(self) -> str:
        d: dict[str, Any] = {
            "ts": self.ts,
            "session": self.session,
            "text": self.text,
            "language": self.language,
            "scope": self.scope,
        }
        if self.tags:
            d["tags"] = self.tags
        return json.dumps(d, ensure_ascii=False)


def read_log(path: str | Path) -> list[Event]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            events.append(
                Event(
                    ts=float(d["ts"]),
                    session=str(d["session"]),
                    text=str(d["text"]),
                    language=str(d.get("language", "en")),
                    scope=str(d.get("scope", "default")),
                    tags=d.get("tags"),
                )
            )
    events.sort(key=lambda e: e.ts)
    return events


def write_log(path: str | Path, events: Iterable[Event]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(e.to_json() + "\n")


DANDA_LANGUAGES = frozenset({"hi", "mr", "ne", "sa", "bn", "as", "pa", "or"})


def _sentence(
    rng: random.Random, words: list[str], length: int, max_chars: int, seen: set[str], language: str
) -> str:
    """Words up to about ``length`` characters, never over ``max_chars`` before the terminator."""
    full_stop = "।" if language in DANDA_LANGUAGES else "."
    while True:
        out: list[str] = []
        while len(" ".join(out)) < length:
            out.append(rng.choice(words))
        if len(out) > 1 and len(" ".join(out)) > max_chars:
            out.pop()
        # A short last word ("a.") reads as an initial to sentencex, not a sentence end.
        while len(out[-1]) < 3:
            out[-1] = rng.choice(words)
        text = " ".join(out)
        if language == "en":
            text = text[0].upper() + text[1:]
        text += full_stop if rng.random() < 0.8 else "?"
        if text not in seen:
            seen.add(text)
            return text


_FILLER_WORD = {"en": "Okay", "hi": "अच्छा", "ta": "சரி"}


def _variant(rng: random.Random, text: str, lang: str) -> str:
    first, _, rest = text.partition(" ")
    if not rest:
        return text
    if rng.random() < 0.5:
        return f"{first}, {rest}"
    filler = _FILLER_WORD.get(lang, "Okay")
    lead = first[0].lower() + first[1:] if lang == "en" else first
    return f"{filler}, {lead} {rest}"


@dataclass
class Corpus:
    turns: list[list[str]]
    repeats: int
    language: str

    @property
    def sentences(self) -> list[str]:
        return [s for t in self.turns for s in t]

    def events(
        self, *, scope: str = "bench", t0: float = 1_792_000_000.0, gap_s: float = 30.0
    ) -> Iterator[Event]:
        """The corpus as a replay log: one event per turn, one session per turn, ``gap_s`` apart."""
        for i, turn in enumerate(self.turns):
            yield Event(t0 + i * gap_s, f"bench-{i:05d}", " ".join(turn), self.language, scope)


def generate(
    n: int = 200,
    r: float = 0.5,
    *,
    seed: int = 1,
    language: str = "en",
    min_chars: int = 20,
    max_chars: int = 160,
    zipf_s: float = 1.0,
    turn_sizes: tuple[int, int] = (1, 5),
    variants: float = 0.0,
) -> Corpus:
    """``n`` sentences of which ``round(n * r)`` repeat an earlier one, Zipf-distributed (9.3).

    Popularity ranks are assigned to the unique sentences at random, so a few of
    them recur often and the rest once or twice. A fraction ``variants`` of the
    repeats are near-duplicates instead of exact ones, half with a comma added
    after the first word (a ``punctuation`` difference) and half with a leading
    filler word (``filler``), for measuring Level B.
    """
    rng = random.Random(seed)
    lang = language.split("-")[0].lower()
    words = VOCAB.get(lang, VOCAB["en"])
    repeats = round(n * r) if n > 1 else 0
    repeats = min(repeats, n - 1)
    unique = n - repeats
    seen: set[str] = set()
    pool = [
        _sentence(rng, words, rng.randint(min_chars, max_chars), max_chars, seen, lang) for _ in range(unique)
    ]
    rank = list(range(unique))
    rng.shuffle(rank)
    weight = [1.0 / (rank[i] + 1) ** zipf_s for i in range(unique)]
    # Positions of repeats: never the first draw; the rest chosen at random.
    repeat_at = set(rng.sample(range(1, n), repeats)) if repeats else set()
    draws: list[str] = []
    introduced = 0
    for pos in range(n):
        if pos in repeat_at and introduced > 0:
            text = rng.choices(pool[:introduced], weight[:introduced])[0]
            if variants and rng.random() < variants:
                text = _variant(rng, text, lang)
            draws.append(text)
        else:
            draws.append(pool[introduced])
            introduced += 1
    turns, i = [], 0
    while i < n:
        size = rng.randint(*turn_sizes)
        turns.append(draws[i : i + size])
        i += size
    return Corpus(turns=turns, repeats=n - introduced, language=language)


# Labelled corpora for the test plan (TEST_PLAN.md 6.1 to 6.4).
#
# A pool is a YAML file of base sentences with slot annotations, one authored
# paraphrase and one authored meaning change each. ``labelled`` assembles a
# deterministic corpus per seed in which every sentence carries a label from
# the oracle of 6.1, relative to the earlier sentences of the same corpus.

REPEAT_LABELS = ("exact", "surface", "punct_inner", "punct_end", "filler", "paraphrase")
DECOY_LABELS = ("slot_change", "meaning_change", "punct_question")
LABELS = ("new", *REPEAT_LABELS, *DECOY_LABELS)
REPEAT_SHARES = {
    "exact": 0.55,
    "surface": 0.10,
    "punct_inner": 0.08,
    "punct_end": 0.02,
    "filler": 0.10,
    "paraphrase": 0.15,
}
DECOY_SHARES = {"slot_change": 0.60, "meaning_change": 0.20, "punct_question": 0.20}
DOMAINS = ("support", "banking", "delivery", "telecom", "appointments")
SLOT_KINDS = ("name", "number", "date", "amount")
POOL_LANGUAGES = ("en", "hi", "bn", "as", "pa", "gu", "mr", "or", "ta", "te", "kn", "ml")
_NATIVE_ZERO = {
    "hi": 0x966, "mr": 0x966, "bn": 0x9E6, "as": 0x9E6, "pa": 0xA66, "gu": 0xAE6,
    "or": 0xB66, "ta": 0xBE6, "te": 0xC66, "kn": 0xCE6, "ml": 0xD66,
}  # fmt: skip


@dataclass(frozen=True)
class Slot:
    span: str
    kind: str
    alts: tuple[str, ...]


@dataclass(frozen=True)
class Base:
    id: str
    domain: str
    text: str
    slots: tuple[Slot, ...]
    paraphrase: str
    meaning_change: str


@dataclass(frozen=True)
class Pool:
    language: str
    bases: tuple[Base, ...]

    @property
    def locale(self) -> str:
        return f"{self.language}-IN"


def load_pool(path: str | Path) -> Pool:
    import yaml

    d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    bases = tuple(
        Base(
            id=str(b["id"]),
            domain=str(b["domain"]),
            text=str(b["text"]),
            slots=tuple(
                Slot(str(s["span"]), str(s["kind"]), tuple(str(a) for a in s["alts"]))
                for s in b.get("slots") or ()
            ),
            paraphrase=str(b["paraphrase"]),
            meaning_change=str(b["meaning_change"]),
        )
        for b in d["bases"]
    )
    return Pool(language=str(d["language"]), bases=bases)


def _one_sentence(text: str, lang: str) -> bool:
    from tts_cache.split import split_text

    parts = split_text(text, lang)
    return len(parts) == 1 and parts[0].strip() == text.strip()


def validate_pool(pool: Pool) -> list[str]:
    """Structural problems of a pool (6.3): each a human-readable string; empty when valid."""
    from tts_cache.canonical import canonicalise

    lang = pool.language
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_canon: dict[str, str] = {}
    for b in pool.bases:
        where = f"{b.id}"
        if b.id in seen_ids:
            problems.append(f"{where}: duplicate id")
        seen_ids.add(b.id)
        if b.domain not in DOMAINS:
            problems.append(f"{where}: domain {b.domain!r} not in {DOMAINS}")
        if not 15 <= len(b.text) <= 160:
            problems.append(f"{where}: length {len(b.text)} outside 15 to 160")
        if b.text.strip()[-1:] not in (".", "?", "!", "।"):
            problems.append(f"{where}: no terminator")
        for name, t in (("text", b.text), ("paraphrase", b.paraphrase), ("meaning_change", b.meaning_change)):
            if not _one_sentence(t, lang):
                problems.append(f"{where}: {name} is not exactly one sentence for the splitter: {t!r}")
        c = canonicalise(b.text, lang)
        if c in seen_canon:
            problems.append(f"{where}: same canonical text as {seen_canon[c]}")
        seen_canon[c] = b.id
        if canonicalise(b.paraphrase, lang) == c:
            problems.append(f"{where}: paraphrase canonicalises to the base")
        if canonicalise(b.meaning_change, lang) == c:
            problems.append(f"{where}: meaning change canonicalises to the base")
        if not b.slots:
            problems.append(f"{where}: no slot annotation")
        for s in b.slots:
            if s.kind not in SLOT_KINDS:
                problems.append(f"{where}: slot kind {s.kind!r}")
            if s.span not in b.text:
                problems.append(f"{where}: slot span {s.span!r} not in the text")
            if s.span not in b.paraphrase:
                problems.append(f"{where}: slot span {s.span!r} not kept in the paraphrase")
            if s.span not in b.meaning_change:
                problems.append(f"{where}: slot span {s.span!r} not kept in the meaning change")
            if len(s.alts) != 3 or len(set(s.alts)) != 3 or s.span in s.alts:
                problems.append(f"{where}: slot {s.span!r} needs three distinct alternatives")
    return problems


# Rule-generated variants. Each returns None when the rule does not apply.


def _terminator(text: str) -> str:
    return text.rstrip()[-1:]


def _statement_end(lang: str) -> str:
    return "।" if lang in DANDA_LANGUAGES else "."


def surface_variants(text: str, lang: str) -> list[str]:
    """Spellings that only the canonicaliser's rules remove (6.1 ``surface``)."""
    import unicodedata

    out: list[str] = []
    words = text.split(" ")
    if len(words) > 2:
        k = len(words) // 2
        out.append(" ".join(words[:k]) + "  " + " ".join(words[k:]))
        out.append(" ".join(words[:k]) + " \u200b" + " ".join(words[k:]))
        out.append(" ".join(words[:k]) + " **" + words[k] + "** " + " ".join(words[k + 1 :]))
    nfd = unicodedata.normalize("NFD", text)
    if nfd != text:
        out.append(nfd)
    end = _terminator(text)
    if end in (".", "।", "!", "?"):
        out.append(text.rstrip()[:-1].rstrip() + " 🙂" + end)
    if end == ".":
        out.append(text.rstrip() + ".")
        if lang in DANDA_LANGUAGES:
            out.append(text.rstrip()[:-1] + "।")
    if end == "।":
        out.append(text.rstrip()[:-1] + ".")
    zero = _NATIVE_ZERO.get(lang)
    if zero is not None and any(ch.isdigit() and ch.isascii() for ch in text):
        out.append("".join(chr(zero + int(ch)) if ch.isascii() and ch.isdigit() else ch for ch in text))
    if lang == "en" and text[:1].isupper():
        out.append(text[0].lower() + text[1:])
    if any("\u0900" <= ch <= "\u0d7f" for ch in text) and "\u094d" in text:
        out.append(text.replace("\u094d", "\u094d\u200c", 1))
    return out


def punct_inner_variants(text: str) -> list[str]:
    words = text.split(" ")
    out = []
    for k in range(1, len(words) - 1):
        w = words[k - 1]
        if w and w[-1].isalnum():
            out.append(" ".join([*words[: k - 1], w + ",", *words[k:]]))
    if "," in text:
        out.append(text.replace(",", "", 1))
    return out


def punct_end_variant(text: str, lang: str) -> str | None:
    end = _terminator(text)
    body = text.rstrip()[:-1]
    if end in (".", "।"):
        return body + "!"
    if end == "!":
        return body + _statement_end(lang)
    return None


def punct_question_variant(text: str, lang: str) -> str | None:
    end = _terminator(text)
    body = text.rstrip()[:-1]
    if end in (".", "।", "!"):
        return body + "?"
    if end == "?":
        return body + _statement_end(lang)
    return None


def filler_variants(text: str, lang: str) -> list[str]:
    from tts_cache.match.lexicon import COMMON_EN, FILLER_PHRASES, FILLERS

    words = sorted(FILLERS.get(lang, frozenset())) + sorted(FILLER_PHRASES.get(lang, frozenset()))
    lead = text
    first = text.split(" ", 1)[0].strip(",.?!").lower()
    if lang == "en" and text[:1].isupper() and first in COMMON_EN and first != "i":
        lead = text[0].lower() + text[1:]
    out = []
    for f in words:
        f = f.strip("'")
        if not f:
            continue
        f = f[0].upper() + f[1:] if lang == "en" else f
        out.append(f"{f}, {lead}")
    return out


def slot_variants(b: Base) -> list[tuple[str, str]]:
    """(text, description) for every single-slot change of ``b``."""
    return [(b.text.replace(s.span, a, 1), f"{s.span}->{a}") for s in b.slots for a in s.alts]


@dataclass(frozen=True)
class LabelledItem:
    index: int
    session: int
    turn: int
    first_in_turn: bool
    text: str
    label: str
    base: str
    note: str = ""


@dataclass
class LabelledCorpus:
    language: str
    seed: int
    r: float
    d: float
    mix: str
    items: list[LabelledItem]

    @property
    def turns(self) -> list[list[str]]:
        out: dict[int, list[str]] = {}
        for it in self.items:
            out.setdefault(it.turn, []).append(it.text)
        return [out[k] for k in sorted(out)]

    @property
    def sessions(self) -> list[list[list[LabelledItem]]]:
        by: dict[int, dict[int, list[LabelledItem]]] = {}
        for it in self.items:
            by.setdefault(it.session, {}).setdefault(it.turn, []).append(it)
        return [[by[s][t] for t in sorted(by[s])] for s in sorted(by)]

    def counts(self) -> dict[str, int]:
        c = {k: 0 for k in LABELS}
        for it in self.items:
            c[it.label] += 1
        return c

    def events(
        self, *, scope: str = "testplan", t0: float = 1_792_000_000.0, turn_gap_s: float = 30.0
    ) -> Iterator[dict[str, Any]]:
        """One replay-log event per sentence, with ``label``, ``base``, ``turn`` and ``first_in_turn``."""
        k = 0
        last_turn = -1
        for it in self.items:
            k = 0 if it.turn != last_turn else k + 1
            last_turn = it.turn
            yield {
                "ts": round(t0 + it.turn * turn_gap_s + k * 0.3, 3),
                "session": f"s-{it.session:04d}",
                "text": it.text,
                "language": f"{self.language}-IN",
                "scope": scope,
                "label": it.label,
                "base": it.base,
                "turn": it.turn,
                "first_in_turn": it.first_in_turn,
            }

    def write(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for e in self.events():
                f.write(json.dumps(e, ensure_ascii=False) + "\n")


def read_labelled(path: str | Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _shares(total: int, shares: dict[str, float]) -> dict[str, int]:
    """Largest-remainder rounding of ``total`` over ``shares``."""
    raw = {k: total * v for k, v in shares.items()}
    out = {k: int(v) for k, v in raw.items()}
    rest = total - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), k))[:rest]:
        out[k] += 1
    return out


def labelled(
    pool: Pool,
    n: int = 200,
    r: float = 0.25,
    d: float = 0.10,
    seed: int = 1,
    *,
    mix: str = "labelled",
    zipf_s: float = 1.0,
    turn_sizes: tuple[int, int] = (1, 4),
    session_turns: tuple[int, int] = (3, 6),
) -> LabelledCorpus:
    """A labelled corpus of ``n`` sentences (6.4); ``mix="exact"`` is the exact-only mix (no decoys).

    ``round(n * r)`` positions are servable repeats and ``round(n * d)`` decoys,
    with labels split by largest remainder over the shares of 6.2; the rest are
    ``new`` and each introduces the next base of a seed-shuffled pool. Repeats
    and decoys draw Zipf-weighted from the bases already introduced. A variant
    whose canonical text already occurred in the corpus is not drawn again, so
    every label holds relative to every earlier sentence. A label that no
    introduced base can take yet waits for the next repeat or decoy position.
    """
    from tts_cache.canonical import canonicalise
    from tts_cache.match.near import classify

    rng = random.Random(seed)
    lang = pool.language
    if mix == "exact":
        d = 0.0
    n_rep = round(n * r)
    n_dec = round(n * d)
    n_new = n - n_rep - n_dec
    if n_new > len(pool.bases):
        raise ValueError(f"{n_new} new sentences need {n_new} bases; the pool has {len(pool.bases)}")
    if mix == "exact":
        rep_labels = {"exact": n_rep}
    else:
        rep_labels = _shares(n_rep, REPEAT_SHARES)
    dec_labels = _shares(n_dec, DECOY_SHARES) if n_dec else {}
    queue = [k for k, v in rep_labels.items() for _ in range(v)] + [
        k for k, v in dec_labels.items() for _ in range(v)
    ]
    rng.shuffle(queue)
    special = set(rng.sample(range(1, n), n_rep + n_dec)) if n_rep + n_dec else set()
    order = list(pool.bases)
    rng.shuffle(order)
    rank = list(range(len(order)))
    rng.shuffle(rank)
    weight = {b.id: 1.0 / (rank[i] + 1) ** zipf_s for i, b in enumerate(order)}
    canon = {b.id: canonicalise(b.text, lang) for b in order}
    used: set[str] = set(canon.values())  # base texts are reserved for their own new and exact draws
    used_variant: set[tuple[str, str]] = set()
    introduced: list[Base] = []
    texts: list[tuple[str, str, str, str]] = []  # text, label, base id, note

    def fresh(cands: list[str], base: Base) -> list[str]:
        return [t for t in cands if canonicalise(t, lang) not in used and _one_sentence(t, lang)]

    def build(label: str, b: Base) -> tuple[str, str] | None:
        c = canon[b.id]
        if label == "exact":
            return b.text, ""
        if label == "surface":
            cands = [
                t
                for t in surface_variants(b.text, lang)
                if t != b.text and canonicalise(t, lang) == c and _one_sentence(t, lang)
            ]
            return (rng.choice(cands), "") if cands else None
        if label in ("paraphrase", "meaning_change"):
            if (label, b.id) in used_variant:
                return None
            t = b.paraphrase if label == "paraphrase" else b.meaning_change
            return (t, "") if canonicalise(t, lang) not in used else None
        if label == "punct_inner":
            cands = [
                t
                for t in fresh(punct_inner_variants(b.text), b)
                if classify(canonicalise(t, lang), c, lang) == "punctuation"
            ]
            return (rng.choice(cands), "") if cands else None
        if label in ("punct_end", "punct_question"):
            v = (punct_end_variant if label == "punct_end" else punct_question_variant)(b.text, lang)
            if v is None or not fresh([v], b) or classify(canonicalise(v, lang), c, lang) != "punctuation":
                return None
            return v, ""
        if label == "filler":
            cands = [
                t
                for t in fresh(filler_variants(b.text, lang), b)
                if classify(canonicalise(t, lang), c, lang, a_text=t, b_text=b.text) == "filler"
            ]
            return (rng.choice(cands), "") if cands else None
        if label == "slot_change":
            cands = [(t, note) for t, note in slot_variants(b) if fresh([t], b)]
            return rng.choice(cands) if cands else None
        raise ValueError(label)

    def draw(label: str) -> tuple[str, str, Base] | None:
        pool_ = list(introduced)
        while pool_:
            b = rng.choices(pool_, [weight[x.id] for x in pool_])[0]
            got = build(label, b)
            if got is not None:
                return got[0], got[1], b
            pool_.remove(b)
        return None

    nxt = 0
    for pos in range(n):
        if pos not in special:
            b = order[nxt]
            nxt += 1
            introduced.append(b)
            texts.append((b.text, "new", b.id, ""))
            continue
        for qi, label in enumerate(queue):
            got = draw(label)
            if got is not None:
                text, note, b = got
                queue.pop(qi)
                if label not in ("exact", "surface"):
                    used.add(canonicalise(text, lang))
                if label in ("paraphrase", "meaning_change"):
                    used_variant.add((label, b.id))
                texts.append((text, label, b.id, note))
                break
        else:
            raise ValueError(f"seed {seed}: no introduced base can take any of {sorted(set(queue))}")
    items: list[LabelledItem] = []
    i, turn, session = 0, 0, 0
    while i < n:
        for _ in range(rng.randint(*session_turns)):
            if i >= n:
                break
            size = rng.randint(*turn_sizes)
            for k, (text, label, bid, note) in enumerate(texts[i : i + size]):
                items.append(LabelledItem(i + k, session, turn, k == 0, text, label, bid, note))
            i += size
            turn += 1
        session += 1
    return LabelledCorpus(lang, seed, r, d, mix, items)


def control(pool: Pool, seed: int = 1, **kw: Any) -> LabelledCorpus:
    """The r = 0 control: every base once, in a seed-shuffled order (nothing can hit)."""
    return labelled(pool, n=len(pool.bases), r=0.0, d=0.0, seed=seed, mix="exact", **kw)
