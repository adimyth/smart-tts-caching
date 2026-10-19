"""Per-script text canonicalisation applied before hashing (SOLUTION.md section 6).

Each rule is a named function in a registry, grouped by script and applied in
the order of section 6. Two strings with the same canonical form must not be
able to sound different through the same provider and voice, so every rule
folds only differences a provider cannot speak.

Changing any rule changes :func:`rules_hash`, and ``SCHEMA_VERSION`` must be
bumped with it, which moves every key to a new scope (a test enforces this).
"""

import hashlib
import inspect
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

SCHEMA_VERSION = 1

# Scripts a language belongs to. A rule tagged "common" runs for every language.
INDIC_LANGUAGES = frozenset({"hi", "mr", "ne", "sa", "bn", "as", "pa", "gu", "or", "ta", "te", "kn", "ml"})


def script_of(language: str) -> str:
    lang = language.replace("_", "-").split("-")[0].lower()
    return "indic" if lang in INDIC_LANGUAGES else "latin"


@dataclass(frozen=True)
class Rule:
    name: str
    scripts: frozenset[str]
    fn: Callable[[str], str]


RULES: list[Rule] = []


def rule(name: str, *scripts: str) -> Callable[[Callable[[str], str]], Callable[[str], str]]:
    def register(fn: Callable[[str], str]) -> Callable[[str], str]:
        RULES.append(Rule(name, frozenset(scripts or ("common",)), fn))
        return fn

    return register


# 1. Unicode form, script-specific normalisation, invisible characters.

# Precomposed nukta letters are composition exclusions, so NFC already decomposes
# them; the rule states it so a future Unicode change cannot slip past.
_NUKTA_PRECOMPOSED = {
    chr(cp): unicodedata.normalize("NFD", chr(cp))
    for cp in [
        *range(0x0958, 0x0960),
        0x09DC,
        0x09DD,
        0x09DF,
        0x0A33,
        0x0A36,
        *range(0x0A59, 0x0A5C),
        0x0A5E,
        0x0B5C,
        0x0B5D,
    ]
}
_ZERO_WIDTH = re.compile("[\u200b\u2060\ufeff\u00ad]")
_JOINERS = re.compile("[\u200c\u200d]")


@rule("nfc")
def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


@rule("indic_nukta", "indic")
def indic_nukta(text: str) -> str:
    return "".join(_NUKTA_PRECOMPOSED.get(ch, ch) for ch in text)


@rule("indic_joiners", "indic")
def indic_joiners(text: str) -> str:
    # ZWJ and ZWNJ select a conjunct's rendering in Indic scripts, not its sound.
    return _JOINERS.sub("", text)


@rule("zero_width")
def zero_width(text: str) -> str:
    return _ZERO_WIDTH.sub("", text)


# 2. Native digits to ASCII.


@rule("ascii_digits")
def ascii_digits(text: str) -> str:
    out = []
    for ch in text:
        if ch > "\x7f" and unicodedata.category(ch) == "Nd":
            out.append(str(unicodedata.decimal(ch)))
        else:
            out.append(ch)
    return "".join(out)


# 3. Terminators unified, punctuation runs collapsed.

_TERMINATORS = str.maketrans({"।": ".", "॥": ".", "。": "."})
_PUNCT_RUN = re.compile(r"([.!?,;:])\1+")


@rule("terminators")
def terminators(text: str) -> str:
    return _PUNCT_RUN.sub(r"\1", text.translate(_TERMINATORS))


# 4. Whitespace, quotes and dashes, emoji, markdown artefacts.

_QUOTES = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
    }
)
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-"})
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, transport, flags, supplemental symbols
    "☀-➿"  # miscellaneous symbols and dingbats
    "⬀-⯿"  # arrows and stars
    "︎️"  # variation selectors
    "⃣"  # combining keycap
    "]+"
)
_MD_EMPHASIS = re.compile(r"[*`]+")
_MD_UNDERSCORES = re.compile(r"_+")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEADING = re.compile(r"^(?:\s*(?:#{1,6}|>)\s+)+")
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.!?,;:।॥。])")


@rule("quotes_dashes")
def quotes_dashes(text: str) -> str:
    return text.translate(_QUOTES).translate(_DASHES)


@rule("emoji")
def emoji(text: str) -> str:
    return _EMOJI.sub(" ", text)


def _underscores(m: re.Match[str]) -> str:
    # Keep an underscore run inside a word ("snake_case"); drop one used as emphasis.
    s, i, j = m.string, m.start(), m.end()
    inside = 0 < i and j < len(s) and s[i - 1].isalnum() and s[j].isalnum()
    return m.group() if inside else ""


@rule("markdown")
def markdown(text: str) -> str:
    text = _MD_EMPHASIS.sub("", text)
    text = _MD_UNDERSCORES.sub(_underscores, text)
    text = _MD_LINK.sub(r"\1", text)
    return _MD_HEADING.sub("", text)


@rule("whitespace")
def whitespace(text: str) -> str:
    text = _SPACE.sub(" ", text).strip()
    return _SPACE_BEFORE_PUNCT.sub(r"\1", text)


# 5. Trailing full stop dropped; "?", "!" and "," change prosody and stay.


@rule("trailing_period")
def trailing_period(text: str) -> str:
    return text[:-1].rstrip() if text.endswith(".") else text


# 6. Lowercase for cased scripts unless the whole sentence is upper case.


@rule("lowercase")
def lowercase(text: str) -> str:
    cased = [ch for ch in text if ch.isupper() or ch.islower()]
    if len(cased) > 1 and all(ch.isupper() for ch in cased):
        return text
    return text.lower()


def rule_names() -> list[str]:
    return [r.name for r in RULES]


def rules_hash() -> str:
    """A hash over every rule's name, scripts, order and source code."""
    h = hashlib.sha256()
    for r in RULES:
        h.update(r.name.encode())
        h.update(",".join(sorted(r.scripts)).encode())
        h.update(inspect.getsource(r.fn).encode())
    for value in (
        _NUKTA_PRECOMPOSED,
        _ZERO_WIDTH.pattern,
        _JOINERS.pattern,
        _PUNCT_RUN.pattern,
        _EMOJI.pattern,
    ):
        h.update(repr(value).encode())
    for pattern in (_MD_EMPHASIS, _MD_UNDERSCORES, _MD_LINK, _MD_HEADING, _SPACE, _SPACE_BEFORE_PUNCT):
        h.update(pattern.pattern.encode())
    for table in (_TERMINATORS, _QUOTES, _DASHES):
        h.update(repr(sorted(table.items())).encode())
    return h.hexdigest()[:16]


# A later rule can expose work for an earlier one ("Great! 🎉!" becomes "Great!!"
# once the emoji goes), so the rules run in order until a pass changes nothing.
# Every rule only removes, folds or maps one way, so two passes almost always suffice.
_MAX_PASSES = 4


def canonicalise(text: str, language: str, *, disabled: Iterable[str] = ()) -> str:
    """The canonical form of ``text`` for ``language``; ``disabled`` names rules to skip."""
    skip = frozenset(disabled)
    script = script_of(language)
    rules = [r.fn for r in RULES if r.name not in skip and ("common" in r.scripts or script in r.scripts)]
    for _ in range(_MAX_PASSES):
        before = text
        for fn in rules:
            text = fn(text)
        if text == before:
            break
    return text
