"""Findings of the test plan against the matching code.

F1 and the negation cases of F5 were fixed on the owner's decision; these
tests keep the pool decoys that exposed them refused. They are in-sample:
the general forms are tested on other sentences in ``test_slots.py``.
"""

import pytest

from tts_cache.canonical import canonicalise
from tts_cache.match.slots import slots

POOL_NEGATIONS = [
    (
        "bn",
        "আপনি কি নতুন রোগী, নাকি আগে আরোগ্য ক্লিনিকে দেখিয়েছেন?",
        "আপনি তো নতুন রোগী নন, আগে আরোগ্য ক্লিনিকে দেখিয়েছেন তো?",
    ),
    (
        "kn",
        "ನಿಮ್ಮ ಮಗನ ಸಿಮ್ ಅನ್ನು ನಿಮ್ಮ ಫ್ಯಾಮಿಲಿ ಪ್ಲಾನ್‌ಗೆ ಸೇರಿಸಬಹುದು, ಹೆಚ್ಚುವರಿ ₹150 ಆಗುತ್ತದೆ.",
        "ನಿಮ್ಮ ಮಗನ ಸಿಮ್ ಅನ್ನು ನಿಮ್ಮ ಫ್ಯಾಮಿಲಿ ಪ್ಲಾನ್‌ಗೆ ಸೇರಿಸಲಾಗುವುದಿಲ್ಲ, ಹೆಚ್ಚುವರಿ ₹150 ಕೊಟ್ಟರೂ.",
    ),
    (
        "kn",
        "17 ನವೆಂಬರ್ ರ ಶಸ್ತ್ರಚಿಕಿತ್ಸೆಗೆ ನಿಮ್ಮ ಜೊತೆ ಒಬ್ಬ ಸಹಾಯಕರು ಬರುತ್ತಾರೆಯೇ?",
        "17 ನವೆಂಬರ್ ರ ಶಸ್ತ್ರಚಿಕಿತ್ಸೆಗೆ ನಿಮ್ಮ ಜೊತೆ ಯಾವ ಸಹಾಯಕರೂ ಬರುವುದಿಲ್ಲವೇ?",
    ),
    (
        "ta",
        "டிவி ஸ்கிரீன்ல எரர் கோட் 301 வந்தா, செட்டாப் பாக்ஸை அன்ப்ளக் பண்ணி மறுபடி போடுங்க.",
        "டிவி ஸ்கிரீன்ல எரர் கோட் 301 வந்தா, செட்டாப் பாக்ஸை அன்ப்ளக் பண்ணாதீங்க.",
    ),
    (
        "te",
        "మీ ఏరియాలో టవర్ మెయింటెనెన్స్ వల్ల సాయంత్రం 6 గంటల వరకు సిగ్నల్ ఉండకపోవచ్చు.",
        "మీ ఏరియాలో టవర్ మెయింటెనెన్స్ ఉన్నా సాయంత్రం 6 గంటల వరకు సిగ్నల్ ఉంటుంది.",
    ),
]


@pytest.mark.parametrize(
    ("lang", "a", "b"), POOL_NEGATIONS, ids=[f"{lang}-{i}" for i, (lang, _, _) in enumerate(POOL_NEGATIONS)]
)
def test_pool_negation_decoys_differ_in_slots(lang: str, a: str, b: str) -> None:
    assert slots(canonicalise(a, lang), lang, a) != slots(canonicalise(b, lang), lang, b)


# F6, fixed on the owner's decision: FILLERS['as'] holds নহয় and FILLERS['or'] ନା, which are also
# negations, so Level B served the affirmative clip for the negated sentence (as-042, as-091, as-146).
@pytest.mark.parametrize(
    ("lang", "base", "negated"),
    [
        ("as", "আপোনাৰ দৈনিক ইউপিআই সীমা ₹1,00,000।", "আপোনাৰ দৈনিক ইউপিআই সীমা ₹1,00,000 নহয়।"),
        ("as", "এই অফাৰটো আপোনাৰ বাবে বৈধ।", "এই অফাৰটো আপোনাৰ বাবে বৈধ নহয়।"),
    ],
)
def test_f6_level_b_never_classifies_a_negation_as_filler(lang: str, base: str, negated: str) -> None:
    from tts_cache.match.near import classify

    got = classify(canonicalise(negated, lang), canonicalise(base, lang), lang, a_text=negated, b_text=base)
    assert got != "filler"
