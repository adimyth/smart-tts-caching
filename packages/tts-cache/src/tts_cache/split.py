"""Sentence splitting on sentencex with Pipecat's look-ahead rules (SOLUTION.md 3 and 6).

:class:`SentenceSplitter` reproduces the boundaries of Pipecat 1.12's
``SimpleTextAggregator`` and ``match_endofsentence`` without importing Pipecat:
a candidate boundary is checked at the first non-space character after a
terminator, retried once when the following word ends, and the buffer is
flushed at the end of the response. On top of sentencex it applies
per-language abbreviation overrides: titles such as "डॉ." never end a
sentence, and company suffixes such as "Inc." end one when a new sentence
visibly starts after them.
"""

import re
from collections.abc import Callable
from enum import Enum, auto
from functools import cache

from sentencex import segment  # pyright: ignore[reportAttributeAccessIssue]

# The terminator set of pipecat.utils.string.SENTENCE_ENDING_PUNCTUATION.
SENTENCE_ENDING_PUNCTUATION = frozenset(".!?;…。？！；．｡।॥؟؛۔؏၊။។៕໌།༎։՜՞።፧፨")
_LATIN_SENTENCE_ENDING_PUNCTUATION = frozenset(".!?;…")
UNAMBIGUOUS_SENTENCE_ENDING_PUNCTUATION = SENTENCE_ENDING_PUNCTUATION - _LATIN_SENTENCE_ENDING_PUNCTUATION
_QUOTED_WORD_END = re.compile(r"(?<!\w)([\"'“‘«‹„‟「『]+)[\w.]+\.$")

# Abbreviations after which a sentence never ends, per language. sentencex
# already knows most English titles; the Indic lists add native titles and the
# Latin ones that appear in code-mixed speech.
_LATIN_TITLES = ["Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "Rs.", "St.", "Sr.", "Jr.", "No.", "Mt.", "Capt."]
NO_SPLIT_AFTER: dict[str, list[str]] = {
    "en": _LATIN_TITLES,
    "hi": [
        *_LATIN_TITLES,
        "डॉ.",
        "डा.",
        "श्री.",
        "श्रीमती.",
        "सुश्री.",
        "कु.",
        "प्रो.",
        "स्व.",
        "पं.",
        "मो.",
        "रु.",
        "सं.",
        "ई.",
    ],
    "ta": [*_LATIN_TITLES, "டாக்டர்.", "டாக்.", "டா.", "திரு.", "திருமதி.", "செல்வி.", "பேரா.", "ரூ.", "எண்."],
    "as": [*_LATIN_TITLES, "Rs.", "ডা.", "শ্ৰী."],
    "bn": [*_LATIN_TITLES, "Rs.", "ডা."],
    "gu": [*_LATIN_TITLES, "Rs.", "ડૉ.", "શ્રી."],
    "kn": [*_LATIN_TITLES, "Rs.", "ಡಾ.", "ಶ್ರೀ."],
    "ml": [*_LATIN_TITLES, "Rs.", "ഡോ.", "ശ്രീ."],
    "mr": [*_LATIN_TITLES, "Rs.", "डॉ.", "श्री."],
    "or": [*_LATIN_TITLES, "Rs.", "ଡା.", "ଶ୍ରୀ."],
    "pa": [*_LATIN_TITLES, "Rs.", "ਡਾ.", "ਸ."],
    "te": [*_LATIN_TITLES, "Rs.", "డా.", "శ్రీ."],
}
# Company suffixes: a sentence ends after one when the next word starts a sentence
# (an upper-case Latin letter, or any letter of an uncased script).
SPLIT_AFTER: dict[str, list[str]] = {
    lang: ["Inc.", "Ltd.", "Corp.", "Co.", "LLC.", "Pvt."]
    for lang in ("en", "hi", "ta", "bn", "as", "pa", "gu", "or", "te", "kn", "ml", "mr")
}


def language_code(language: str | None) -> str:
    """Pipecat's ``resolve_sentence_tokenizer_language``: lowercase base code, English by default."""
    if not isinstance(language, str):
        return "en"
    normalized = language.strip().lower().replace("_", "-").split("-")[0]
    return "en" if normalized in ("", "auto") else normalized


def _ends_with_any(text: str, words: list[str]) -> bool:
    for w in words:
        if text.endswith(w) and (len(text) == len(w) or not text[-len(w) - 1].isalnum()):
            return True
    return False


def _starts_sentence(ch: str) -> bool:
    return ch.isupper() or (ch.isalpha() and not ch.islower() and not ch.isascii())


@cache
def _split_pattern(lang: str) -> re.Pattern[str] | None:
    words = SPLIT_AFTER.get(lang)
    if not words:
        return None
    alternatives = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?=\s+\S)")


@cache
def tokenizer(language: str = "en") -> Callable[[str], list[str]]:
    """Sentences of ``text`` with Pipecat's offsets (trailing whitespace stays with the next one)."""
    code = language_code(language)
    no_split = NO_SPLIT_AFTER.get(code, [])
    split_at = _split_pattern(code)

    def raw(text: str) -> list[str]:
        sentences: list[str] = []
        pending = ""
        for span in segment(code, text):
            pending += span
            if span.strip():
                sentence = pending.rstrip()
                sentences.append(sentence)
                pending = pending[len(sentence) :]
        return sentences

    def tokenize(text: str) -> list[str]:
        sentences = raw(text)
        # Merge a split that sentencex made after a title. Each sentence carries
        # its leading whitespace, so joining them restores the original text.
        merged: list[str] = []
        for s in sentences:
            if merged and _ends_with_any(merged[-1], no_split):
                merged[-1] += s
            else:
                merged.append(s)
        # Split after a company suffix that sentencex kept inside a sentence.
        if split_at is None:
            return merged
        out: list[str] = []
        for s in merged:
            start = 0
            for m in split_at.finditer(s):
                rest = s[m.end() :].lstrip()
                if rest and _starts_sentence(rest[0]):
                    out.append(s[start : m.end()])
                    start = m.end()
            out.append(s[start:])
        return out

    return tokenize


def match_endofsentence(text: str, *, language: str | None = None) -> int:
    """The end of the first sentence in ``text``, or 0; Pipecat's rules plus the overrides."""
    text = text.rstrip()
    if not text:
        return 0
    tok = tokenizer(language_code(language))
    sentences = tok(text)
    if not sentences:
        return 0
    first = sentences[0]
    tokenizer_text = text
    if len(sentences) > 1 and (quote := _QUOTED_WORD_END.search(first)):
        start, end = quote.span(1)
        tokenizer_text = text[:start] + " " * (end - start) + text[end:]
        sentences = tok(tokenizer_text)
        first = sentences[0]
    if len(sentences) == 1 and first == tokenizer_text:
        if text[-1] in SENTENCE_ENDING_PUNCTUATION:
            return len(text)
        for i, ch in enumerate(text):
            if ch in UNAMBIGUOUS_SENTENCE_ENDING_PUNCTUATION:
                return i + 1
        return 0
    if len(sentences) > 1:
        return len(first)
    return 0


class _Lookahead(Enum):
    IDLE = auto()
    AWAITING_CHARACTER = auto()
    AWAITING_WORD = auto()
    IN_WORD = auto()


class SentenceSplitter:
    """Turns a token stream into sentences, the unit of caching."""

    def __init__(self, language: str = "en") -> None:
        self.language = language_code(language)
        self._text = ""
        self._state = _Lookahead.IDLE

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return the sentences it completes."""
        out = []
        for ch in text:
            self._text += ch
            if (s := self._check(ch)) is not None:
                out.append(s)
        return out

    def _check(self, ch: str) -> str | None:
        is_punct = ch in SENTENCE_ENDING_PUNCTUATION
        result = None
        if self._advance(ch, is_punct):
            candidate = self._text[:-1] if is_punct else self._text
            eos = match_endofsentence(candidate, language=self.language)
            if eos:
                result = self._text[:eos].strip(" ")
                self._text = self._text[eos:]
                self._state = _Lookahead.IDLE
        if is_punct:
            self._state = _Lookahead.AWAITING_CHARACTER
        return result

    def _advance(self, ch: str, is_punct: bool) -> bool:
        match self._state:
            case _Lookahead.IDLE:
                return False
            case _Lookahead.AWAITING_CHARACTER:
                if ch.isspace() or is_punct:
                    return False
                self._state = _Lookahead.IN_WORD if ch.isalnum() else _Lookahead.AWAITING_WORD
                return True
            case _Lookahead.AWAITING_WORD:
                if ch.isalnum():
                    self._state = _Lookahead.IN_WORD
                return False
            case _Lookahead.IN_WORD:
                if ch.isspace() or is_punct:
                    self._state = _Lookahead.IDLE
                    return True
                return False

    def flush(self) -> str | None:
        """The rest of the buffer at the end of a response."""
        text, self._text, self._state = self._text, "", _Lookahead.IDLE
        return text.strip(" ") if text else None

    def reset(self) -> None:
        self._text = ""
        self._state = _Lookahead.IDLE

    @property
    def pending(self) -> str:
        return self._text


def split_text(text: str, language: str = "en") -> list[str]:
    """Every sentence of a complete text, as the streaming splitter would emit them."""
    s = SentenceSplitter(language)
    out = s.feed(text)
    if (rest := s.flush()) and rest.strip():
        out.append(rest)
    return out
