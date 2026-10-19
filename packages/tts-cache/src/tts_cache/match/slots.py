"""Slot extraction and the Level C slot veto (SOLUTION.md 5).

A *slot* is a token that carries a fact the audio must get right: a number
(order id, OTP, quantity), an amount, a date or time word, a proper name, or a
word written in a script other than the sentence's language (a brand, a
loanword, a name). An embedding treats a sentence with one changed slot as
almost identical to the original, which is exactly the case where serving a
stored clip is wrong. The veto is script-aware and language-blind in its
mechanics: Unicode categories and script ranges find digits and foreign
tokens, and the per-language lexicons supply date words, currency words and
common names.

:func:`slots` returns a frozenset of tagged values, so two sentences with the
same facts in a different order or wording compare equal, and any change,
addition or removal of a fact compares unequal. :func:`same_slots` is the
veto itself.
"""

import difflib
import re
import unicodedata

from tts_cache.match.lexicon import DATES, NAMES, NUMBER_WORDS, has_word, is_negation, lang_of
from tts_cache.match.near import _capitalised, _is_filler, _strip, tokens

_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
_CURRENCY_SIGNS = "₹$€£¥"
# Date words that only mean a time when they follow a number ("10 am"), not on their own ("I am").
_AFTER_NUMBER_ONLY = frozenset({"am", "pm"})
# Lexicon date words that name the concept, not a value: "the date", "o'clock".
_GENERIC_DATE_WORDS = frozenset(
    """
    आठवडा तारखेला तारीख तारीख़ तेदी दिनांक दिवशी दिवस बजे महिना रोजी वाजता वाजेपर्यंत वेळ वेळी চন
    চনত টা টায় তারিখ তারিখে তাৰিখ তাৰিখে দিন বছর বছৰ বজাত বজালৈ বজাৰ বাজে মাস মাহ সপ্তাহ সময় সাল
    ਟਾਈਮ ਤਰੀਕ ਤਾਰੀਖ ਮਹੀਨਾ ਵਜੇ ਸਮਾਂ ਸਾਲ ਹਫ਼ਤਾ ਹਫ਼ਤੇ અઠવાડિયું અઠવાડિયે તારીખ તારીખે દિવસ દિવસે મહિને
    મહિનો વર્ષ વર્ષે વાગે વાગ્યા વાગ્યે સમય સમયે ଟା ଟାରେ ତାରିଖ ତାରିଖରେ ଦିନ ମାସ ସପ୍ତାହ ସମୟ ସମୟରେ தேதி
    மணி மணிக்கு గంట గంటల గంటలకు గంటలు తేదీ తేదీన నెల రోజు వారం సమయం ಗಂಟೆ ಗಂಟೆಗೆ ಗಂಟೆಯ ತಾರೀಖಿಗೆ
    ತಾರೀಖು ತಿಂಗಳು ದಿನಾಂಕ ವರ್ಷ ವಾರ ಸಮಯ ആഴ്ച തീയതി ദിവസം മണി മണിക്ക് മാസം സമയം
    now अभी এখন এখনই এতিয়া ഇപ്പോൾ ଏବେ ବର୍ତ୍ତମାନ இப்போ இப்போது ఇప్పుడు ಈಗ आता હવે ਹੁਣ
    """.split()
)
# Tamil case and quotative suffixes that attach to names and date words.
_TA_SUFFIXES = ("க்குள்", "கிட்ட", "ுக்கு", "க்கு", "ோட", "ன்னு", "ா")

# Primary script per language, as inclusive code point ranges.
_SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "en": ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F)),
    "hi": ((0x900, 0x97F), (0xA8E0, 0xA8FF)),
    "as": ((0x980, 0x9FF),),
    "mr": ((0x900, 0x97F),),
    "bn": ((0x980, 0x9FF),),
    "pa": ((0xA00, 0xA7F),),
    "gu": ((0xA80, 0xAFF),),
    "or": ((0xB00, 0xB7F),),
    "ta": ((0xB80, 0xBFF),),
    "te": ((0xC00, 0xC7F),),
    "kn": ((0xC80, 0xCFF),),
    "ml": ((0xD00, 0xD7F),),
}


def _letters(token: str) -> list[str]:
    return [ch for ch in token if unicodedata.category(ch)[0] == "L"]


def _foreign(token: str, lang: str) -> bool:
    """True when the token's letters fall outside the language's primary script."""
    ranges = _SCRIPT_RANGES.get(lang)
    letters = _letters(token)
    if ranges is None or not letters:
        return False
    return all(not any(lo <= ord(ch) <= hi for lo, hi in ranges) for ch in letters)


def _ascii_digits(token: str) -> str:
    return "".join(str(unicodedata.decimal(ch)) if unicodedata.category(ch) == "Nd" else ch for ch in token)


def _root(tok: str, words: frozenset[str]) -> str:
    """The listed word ``tok`` starts with, so suffixed forms compare equal to the bare one."""
    if tok in words:
        return tok
    hits = [w for w in words if len(w) >= 3 and tok.startswith(w)]
    return max(hits, key=len) if hits else tok


def _stem(tok: str, lang: str) -> str:
    if lang == "ta":
        for suf in _TA_SUFFIXES:
            if tok.endswith(suf) and len(tok) > len(suf) + 2:
                return tok[: -len(suf)]
    return tok


def slots(canonical: str, language: str, text: str | None = None) -> frozenset[str]:
    """The tagged facts in a sentence.

    ``n:`` numbers, ``c:`` currency, ``d:`` dates and times, ``p:`` names,
    ``x:`` words in a script other than the language's own.

    ``canonical`` is the canonical text (digits already ASCII, lower case);
    ``text`` is the original when available, so Latin capitalisation can mark
    names the lexicon does not list.
    """
    lang = lang_of(language)
    out: set[str] = set()
    _, caps = _capitalised(text) if text else (set(), set())
    dates = (DATES.get(lang, frozenset()) | DATES["en"]) - _GENERIC_DATE_WORDS
    names = NAMES.get(lang, frozenset()) | NAMES["en"]
    raws = [r for r in (_ascii_digits(w).lower() for w in canonical.split()) if _strip(r)]
    toks = [_strip(r) for r in raws]
    for i, tok in enumerate(toks):
        prev = toks[i - 1] if i else ""
        stem = _stem(tok, lang)
        for num in _NUMBER.findall(tok):
            out.add("n:" + num.replace(",", ""))
        # A currency sign or word is a fact only beside a number; "the amount" alone is wording.
        if _NUMBER.search(tok) and any(c in raws[i] for c in _CURRENCY_SIGNS):
            out.add("c:" + next(c for c in _CURRENCY_SIGNS if c in raws[i]))
        if tok in _AFTER_NUMBER_ONLY:
            if _NUMBER.search(prev):
                out.add("d:" + tok)
        elif has_word(tok, dates) or stem in dates:
            out.add("d:" + next((w for w in (tok, stem) if w in dates), _root(tok, dates)))
        if tok in NUMBER_WORDS.get(lang, frozenset()):
            out.add("n:" + tok)
        is_name = has_word(tok, names) or stem in names or (tok in caps and tok.isascii() and len(tok) > 1)
        if is_name and not has_word(tok, dates) and stem not in dates:
            out.add("p:" + _root(stem, names))
        elif _foreign(tok, lang):
            out.add("x:" + tok)
        if is_negation(tok, lang):
            out.add("neg")
    return frozenset(out)


def span_veto(a: str, b: str, language: str = "en", max_tokens: int = 2) -> bool:
    """True when ``b`` differs from ``a`` in one short contiguous span that looks like a changed fact.

    Two shapes qualify. A substitution of one or two tokens (a name, a day,
    "12 मई"), or of up to four tokens when a digit is involved. And an
    insertion or deletion of one or two tokens that are not fillers: "was
    successful" against "was not successful" is a negation, not a paraphrase,
    while "okay, …" is Level B's business. An otherwise identical sentence with
    such a span changed is a near-miss, never a paraphrase, which rewrites
    several places at once. A single substituted span of three or more words
    with no digit is a rewording: in agglutinative languages a whole clause is
    two or three words, and refusing those cost Kannada, Malayalam and Telugu a
    fifth of their paraphrases. The veto needs no lexicon for names, so it
    catches the ones the slot detector cannot see.
    """
    ta, tb = tokens(a), tokens(b)
    if ta == tb:
        return False
    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    ops = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    if len(ops) != 1:
        return False
    tag, i1, i2, j1, j2 = ops[0]
    span = ta[i1:i2] + tb[j1:j2]
    longest = max(i2 - i1, j2 - j1)
    if tag == "replace":
        return longest <= max_tokens or (longest <= 4 and any(_NUMBER.search(t) for t in span))
    return longest <= max_tokens and not _is_filler(span, lang_of(language))


def same_slots(
    a: str, b: str, language: str, *, a_text: str | None = None, b_text: str | None = None
) -> bool:
    """The veto: True only when both sentences carry exactly the same facts."""
    return slots(a, language, a_text) == slots(b, language, b_text)
