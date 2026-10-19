"""The slot detector and the Level C slot veto (SOLUTION.md 5)."""

from pathlib import Path

import numpy as np
import pytest
import yaml

from tts_cache.canonical import canonicalise
from tts_cache.match.semantic import SemanticMatcher
from tts_cache.match.slots import same_slots, slots, span_veto
from tts_cache.scope import Scope

FIXTURES = Path(__file__).parent / "fixtures" / "semantic"
LANGS = tuple(sorted(p.stem for p in FIXTURES.glob("*.yaml")))


def load(lang: str) -> list[dict]:
    return yaml.safe_load((FIXTURES / f"{lang}.yaml").read_text(encoding="utf-8"))


def s(text: str, lang: str) -> frozenset[str]:
    return slots(canonicalise(text, lang), lang, text)


def same(p: dict, lang: str) -> bool:
    a, b = canonicalise(p["a"], lang), canonicalise(p["b"], lang)
    return same_slots(a, b, lang, a_text=p["a"], b_text=p["b"])


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [
        ("Your order number 48213 has been shipped.", "en", {"n:48213"}),
        ("Your current account balance is ₹12,450.", "en", {"n:12450", "c:₹"}),
        (
            "Your delivery is scheduled for Tuesday between 9 am and 1 pm.",
            "en",
            {"d:tuesday", "n:9", "d:am", "n:1", "d:pm"},
        ),
        ("I am sorry for the inconvenience.", "en", set()),  # "I" and "am" are not slots
        ("Our delivery partner Arjun will call you.", "en", {"p:arjun"}),
        ("Thanks, Alex.", "en", {"p:alex"}),
        ("आपका मासिक बिल ₹७९९ का है।", "hi", {"n:799", "c:₹"}),  # Devanagari digits
        ("टेक्नीशियन शनिवार दोपहर 2 बजे आपके घर आएगा।", "hi", {"d:शनिवार", "d:दोपहर", "n:2"}),
        ("प्रिया जी, आपका ऑर्डर आज डिलीवर हो जाएगा।", "hi", {"p:प्रिया", "d:आज"}),  # lexicon name
        ("आपकी कैब के ड्राइवर का नाम इमरान है।", "hi", set()),  # a name outside the lexicon: the span veto's job
        ("डॉक्टर अभी दूसरे मरीज़ के साथ हैं।", "hi", set()),
        ("आपका ऑर्डर Amazon से आया है।", "hi", {"p:amazon"}),  # capitalised Latin word inside Hindi
        ("आपका ऑर्डर flipkart से आया है।", "hi", {"x:flipkart"}),  # any Latin word inside Hindi
        ("உங்க பார்சல் புதன்கிழமைக்குள் வந்து சேரும்.", "ta", {"d:புதன்கிழமை"}),  # case suffix stripped
        ("நான் அருண்கிட்ட தான் பேசுறேனா?", "ta", {"p:அருண்"}),
        ("தயவுசெய்து அந்த ஓடிபியை என்கிட்ட சொல்லுங்க.", "ta", set()),  # pronoun, not a name
        ("உங்க இந்த மாச மொபைல் பில் 849 ரூபாய்.", "ta", {"n:849"}),  # the currency word alone is wording
    ],
)
def test_slot_extraction(text: str, lang: str, expected: set[str]) -> None:
    assert s(text, lang) == frozenset(expected)


def test_slots_are_order_and_wording_independent() -> None:
    assert same_slots(
        canonicalise("Your EMI of ₹4,200 is due on the 5th of every month.", "en"),
        canonicalise("On the 5th of each month your ₹4,200 EMI falls due.", "en"),
        "en",
    )
    assert not same_slots(
        canonicalise("Your EMI of ₹4,200 is due on the 5th.", "en"),
        canonicalise("Your EMI of ₹4,300 is due on the 5th.", "en"),
        "en",
    )


# Near-misses the slot detector alone cannot see: an Indic-script name outside
# the lexicon. Latin names are caught by capitalisation; the span veto catches
# the rest (test_the_span_veto_closes_the_known_gaps).
KNOWN_GAPS = {
    "hi": {
        "दीपिका जी, आपकी शिकायत बंद कर दी गई है।",
        "आपका टेस्ट डॉक्टर कपूर की लैब में होगा।",
        "यह अकाउंट किरण पटेल के नाम पर रजिस्टर्ड है।",
        "आपकी कैब के ड्राइवर का नाम इमरान है।",
        "आपका केस हमारी सीनियर एग्ज़ीक्यूटिव रश्मि देख रही हैं।",
        "आपकी क्लेम फाइल अब हरीश जी के पास है।",
        "मैं मीनाक्षी, आपकी बीमा सलाहकार बोल रही हूँ।",
        "डॉक्टर अय्यर आज छुट्टी पर हैं, इसलिए डॉक्टर रेड्डी आपको देखेंगे।",
        "प्रकाश जी, क्या आप अपना पता कन्फर्म कर सकते हैं?",
        "आपके बेटे आरव का नाम पॉलिसी में जोड़ दिया गया है।",
    },
    "ta": {
        "இந்த பார்சலை செல்வி கையெழுத்து போட்டு வாங்கியிருக்காங்க.",
        "நான் இப்போ உங்க காலை கணேஷுக்கு டிரான்ஸ்ஃபர் பண்றேன்.",
        "இந்த பணம் வெங்கடேஷ் என்பவரோட அக்கவுண்ட்டுக்கு போயிருக்கு.",
        "டாக்டர் ராமன் கிட்ட உங்க அப்பாயிண்ட்மெண்ட் கன்ஃபர்ம் ஆகிடுச்சு.",
        "நாளைக்கு உங்க வீட்டுக்கு வர்ற டெக்னீஷியன் பெயர் ஜெயக்குமார்.",
        "எங்க ரெக்கார்டுல உங்க பெயர் மாலதின்னு இருக்கு.",
        "நீங்க கேட்ட பார்சல் யுவராஜ் கிட்ட ஒப்படைக்கப்பட்டது.",
    },
}


@pytest.mark.parametrize("lang", LANGS)
def test_every_number_date_and_amount_near_miss_is_vetoed(lang: str) -> None:
    """The slot veto alone catches nearly all; with the span veto, all.

    "This week" against "next week" carries no slot the detector can see.
    """
    rows = [p for p in load(lang) if p["label"] == "near_miss" and p["slot"] != "name"]
    slot_missed = [p for p in rows if same(p, lang)]
    assert len(slot_missed) <= len(rows) // 10, [p["a"] for p in slot_missed]
    for p in slot_missed:
        assert span_veto(canonicalise(p["a"], lang), canonicalise(p["b"], lang)), p


@pytest.mark.parametrize("lang", ("en", "hi", "ta"))
def test_name_near_misses_are_vetoed_by_slots_except_the_known_gaps(lang: str) -> None:
    names = [p for p in load(lang) if p["label"] == "near_miss" and p["slot"] == "name"]
    missed = {p["a"] for p in names if same(p, lang)}
    assert missed == KNOWN_GAPS.get(lang, set())


@pytest.mark.parametrize("lang", LANGS)
def test_the_span_veto_closes_the_known_gaps(lang: str) -> None:
    """Together the two vetoes stop every near-miss in the fixtures, lexicon or not."""
    for p in load(lang):
        if p["label"] == "near_miss":
            a, b = canonicalise(p["a"], lang), canonicalise(p["b"], lang)
            assert span_veto(a, b) or not same(p, lang), p


def test_span_veto_shape() -> None:
    assert span_veto("your order ships on monday", "your order ships on tuesday")
    assert span_veto("thanks alex", "thanks sam")
    assert span_veto("मैं मीनाक्षी आपकी बीमा सलाहकार बोल रही हूँ", "मैं रचना आपकी बीमा सलाहकार बोल रही हूँ")
    # A paraphrase changes more than one place, or adds and removes rather than substitutes.
    assert not span_veto(
        "i am sorry for the inconvenience this has caused you",
        "i apologise for the trouble this has caused you",
    )
    assert not span_veto("your order ships on monday", "on monday your order ships")
    assert not span_veto("okay your order ships on monday", "your order ships on monday")
    assert not span_veto("same text", "same text")
    # A three-word clause swapped for another is a rewording, not a fact: common in Dravidian languages.
    assert not span_veto("ನಿಮ್ಮ ಪಾವತಿ ಯಶಸ್ವಿಯಾಗಿದೆ", "ನಿಮ್ಮ ಪೇಮೆಂಟ್ ಸಕ್ಸಸ್ಫುಲ್ ಆಗಿದೆ")
    # Unless a digit is in it.
    assert span_veto("your balance is ₹ 2,349 today", "your balance is ₹ 2,439 today")


@pytest.mark.parametrize("lang", LANGS)
def test_veto_lets_most_paraphrases_through(lang: str) -> None:
    """The veto costs recall only where a paraphrase swaps a date word or hits a name homograph."""
    vetoed = sum(not same(p, lang) for p in load(lang) if p["label"] == "paraphrase")
    assert vetoed <= 15, vetoed


class SameVector:
    """An encoder that calls every sentence identical: the worst case for the veto to catch."""

    def encode(self, sentences):
        return np.ones((len(sentences), 8), dtype=np.float32)


def test_matcher_refuses_a_changed_slot_even_at_similarity_one() -> None:
    sc = Scope(service="s", model="m", voice="v", language="en-IN", sample_rate=24000)
    matcher = SemanticMatcher({"en": "stub"}, threshold=0.5, encoder=SameVector())
    stored = "Your consultation is on Thursday at noon."
    matcher.add(sc, "k1", canonicalise(stored, "en"), stored)
    query = "Your consultation is on Tuesday at noon."
    assert matcher.probe(sc, canonicalise(query, "en"), query) is None
    assert matcher.vetoed == 1
    # A real paraphrase rewrites more than one span, so the span veto lets it through.
    same = "Thursday at noon is when your consultation takes place."
    found = matcher.probe(sc, canonicalise(same, "en"), same)
    assert found is not None and found[0].key == "k1"


# Negation decoys: an encoder scores each pair at 0.93 to 0.97, above the floor.
NEGATIONS = [
    ("en", "Your payment was successful.", "Your payment was not successful."),
    ("en", "Your order has been shipped.", "Your order has not been shipped yet."),
    ("en", "I was able to find your account.", "I was unable to find your account."),
    ("hi", "आपका भुगतान सफल रहा।", "आपका भुगतान सफल नहीं रहा।"),
    ("hi", "आपका ऑर्डर भेज दिया गया है।", "आपका ऑर्डर अभी नहीं भेजा गया है।"),
    ("hi", "कृपया OTP किसी को बताएँ।", "कृपया OTP किसी को न बताएँ।"),
    ("ta", "உங்கள் பணம் செலுத்தப்பட்டது.", "உங்கள் பணம் செலுத்தப்படவில்லை."),
    ("te", "మీ చెల్లింపు పూర్తయింది.", "మీ చెల్లింపు పూర్తి కాలేదు."),
    ("kn", "ನಿಮ್ಮ ಪಾವತಿ ಯಶಸ್ವಿಯಾಗಿದೆ.", "ನಿಮ್ಮ ಪಾವತಿ ಯಶಸ್ವಿಯಾಗಿಲ್ಲ."),
    ("ml", "നിങ്ങളുടെ പേയ്‌മെന്റ് വിജയിച്ചു.", "നിങ്ങളുടെ പേയ്‌മെന്റ് വിജയിച്ചില്ല."),
    ("bn", "আপনার পেমেন্ট সফল হয়েছে।", "আপনার পেমেন্ট সফল হয়নি।"),
    ("mr", "तुमचे पेमेंट यशस्वी झाले.", "तुमचे पेमेंट यशस्वी झाले नाही."),
    ("gu", "તમારું પેમેન્ટ સફળ થયું.", "તમારું પેમેન્ટ સફળ થયું નથી."),
    ("pa", "ਤੁਹਾਡਾ ਭੁਗਤਾਨ ਸਫਲ ਰਿਹਾ।", "ਤੁਹਾਡਾ ਭੁਗਤਾਨ ਸਫਲ ਨਹੀਂ ਰਿਹਾ।"),
    ("or", "ଆପଣଙ୍କ ପେମେଣ୍ଟ ସଫଳ ହୋଇଛି।", "ଆପଣଙ୍କ ପେମେଣ୍ଟ ସଫଳ ହୋଇନାହିଁ।"),
    ("as", "আপোনাৰ পেমেণ্ট সফল হৈছে।", "আপোনাৰ পেমেণ্ট সফল হোৱা নাই।"),
]


@pytest.mark.parametrize(
    ("lang", "a", "b"), NEGATIONS, ids=[f"{lang}-{i}" for i, (lang, _, _) in enumerate(NEGATIONS)]
)
def test_negation_is_never_a_paraphrase(lang: str, a: str, b: str) -> None:
    x, y = canonicalise(a, lang), canonicalise(b, lang)
    assert not same_slots(x, y, lang, a_text=a, b_text=b) or span_veto(x, y, lang)


# Negation forms a word list of the observed tokens missed: Bengali negative copulas, Kannada
# vowel-sign and fused ಅಲ್ಲ endings and the negative imperative ‑ಬೇಡಿ, the Tamil negative
# imperative, future negative ‑ாது and fused ‑இல்லை, the Telugu negative modal, ‑లేను/‑లేము,
# ‑కూడదు and the negative imperative ‑కండి.
NEGATION_FORMS = [
    ("bn", "আমি এই অর্ডারের মালিক।", "আমি এই অর্ডারের মালিক নই।"),
    ("bn", "আপনি এই প্ল্যানের যোগ্য।", "আপনি এই প্ল্যানের যোগ্য নন।"),
    ("bn", "তুমি এই অফারের জন্য যোগ্য।", "তুমি এই অফারের জন্য যোগ্য নও।"),
    ("kn", "ಈ ಕೂಪನ್ ಇಂದು ಕೆಲಸ ಮಾಡುತ್ತದೆ.", "ಈ ಕೂಪನ್ ಇಂದು ಕೆಲಸ ಮಾಡುವುದಿಲ್ಲ."),
    ("kn", "ಈ ಪಾರ್ಸೆಲ್ ನಿಮ್ಮದು.", "ಈ ಪಾರ್ಸೆಲ್ ನಿಮ್ಮದಲ್ಲ."),
    ("kn", "ನಿಮ್ಮ ಪಾಸ್‌ವರ್ಡ್ ಬದಲಾಗಿದೆಯೇ?", "ನಿಮ್ಮ ಪಾಸ್‌ವರ್ಡ್ ಬದಲಾಗಿಲ್ಲವೇ?"),
    ("kn", "ಓಟಿಪಿ ಬರ್ತಿದೆಯಾ?", "ಓಟಿಪಿ ಬರ್ತಿಲ್ಲವಾ?"),
    ("ta", "இந்த லிங்கை க்ளிக் பண்ணுங்க.", "இந்த லிங்கை க்ளிக் பண்ணாதீங்க."),
    ("ta", "உங்கள் கார்டை மற்றவர்களிடம் கொடுங்கள்.", "உங்கள் கார்டை மற்றவர்களிடம் கொடுக்காதீர்கள்."),
    ("ta", "கதவைத் திற.", "கதவைத் திறக்காதே."),
    ("te", "ఈ ఆఫర్ మీకు వర్తించవచ్చు.", "ఈ ఆఫర్ మీకు వర్తించకపోవచ్చు."),
    ("te", "మీ రీఫండ్ ఈ వారం జమ కావచ్చు.", "మీ రీఫండ్ ఈ వారం జమ కాకపోవచ్చు."),
    ("kn", "ಈ ಲಿಂಕ್ ಅನ್ನು ತೆರೆಯಿರಿ.", "ಈ ಲಿಂಕ್ ಅನ್ನು ತೆರೆಯಬೇಡಿ."),
    ("kn", "ಬಾಗಿಲು ಮುಚ್ಚಿ.", "ಬಾಗಿಲು ಮುಚ್ಚಬೇಡಿ."),
    ("ta", "உங்கள் பணம் இன்று வரும்.", "உங்கள் பணம் இன்று வராது."),
    ("ta", "இதற்கு ஆவணம் தேவை.", "இதற்கு ஆவணம் தேவையில்லை."),
    ("ta", "இந்த ஃபார்மை நிரப்புங்கள்.", "இந்த ஃபார்மை நிரப்பாதீர்."),
    ("te", "ఈ లింక్ తెరవండి.", "ఈ లింక్ తెరవకండి."),
    ("te", "ఈ ఫారం తీసుకోండి.", "ఈ ఫారం తీసుకోకండి."),
    ("te", "నేను ఈ రోజు రీఫండ్ చేయగలను.", "నేను ఈ రోజు రీఫండ్ చేయలేను."),
    ("te", "ఈ కార్డును ఆన్‌లైన్‌లో వాడాలి.", "ఈ కార్డును ఆన్‌లైన్‌లో వాడకూడదు."),
]


@pytest.mark.parametrize(
    ("lang", "a", "b"), NEGATION_FORMS, ids=[f"{lang}-{i}" for i, (lang, _, _) in enumerate(NEGATION_FORMS)]
)
def test_negation_forms_mark_the_sentence(lang: str, a: str, b: str) -> None:
    assert "neg" not in slots(canonicalise(a, lang), lang, a)
    assert "neg" in slots(canonicalise(b, lang), lang, b)


@pytest.mark.parametrize(
    "text", ["ಅವರೆಲ್ಲ ನಾಳೆ ಬರುತ್ತಾರೆ.", "ಎಲ್ಲ ದಾಖಲೆಗಳನ್ನು ತನ್ನಿ.", "ಬೆಲ್ಲ ಮತ್ತು ಬೇಳೆ ಬಂದಿದೆ.", "ಅಂಗಡಿಯಲ್ಲಿ ಸಿಗುತ್ತದೆ."]
)
def test_kannada_all_and_locative_are_not_negation(text: str) -> None:
    assert "neg" not in slots(canonicalise(text, "kn"), "kn", text)


# Affirmatives that share an ending with a negative form: the Kannada ability auxiliary ಬಲ್ಲ,
# Telugu affirmative imperatives of stems ending in క, and Tamil words ending like ‑இல்லை or ‑ாது.
@pytest.mark.parametrize(
    ("lang", "text"),
    [
        ("kn", "ಅವನು ಇದನ್ನು ಇಂದೇ ಮಾಡಬಲ್ಲ."),
        ("te", "ఈ బటన్ నొక్కండి."),
        ("te", "ఈ తాడును పీకండి."),
        ("ta", "உங்கள் பில்லை சரிபார்க்கவும்."),
        ("ta", "பொது விடுமுறை நாளை."),
    ],
)
def test_affirmatives_sharing_a_negative_ending(lang: str, text: str) -> None:
    assert "neg" not in slots(canonicalise(text, lang), lang, text)


def test_insertion_veto_spares_fillers() -> None:
    # A leading filler is Level B's punctuation-and-filler business, not a changed fact.
    assert not span_veto("your refund has been processed", "okay, your refund has been processed", "en")
    assert not span_veto("आपका रिफंड हो गया है", "अच्छा, आपका रिफंड हो गया है", "hi")
    # One or two inserted content words are.
    assert span_veto("your refund has been processed", "your refund has not been processed", "en")
    assert span_veto("your refund has been processed", "your refund has been processed twice", "en")
    # Three or more inserted words are a rewording.
    assert not span_veto(
        "your refund has been processed", "your refund has been processed by our billing team", "en"
    )
