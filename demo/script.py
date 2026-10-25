"""The launch demo's conversation: persona, pinned lines, scripted turns, caller lines and expected outcomes.

The agent is Ananya at the appointment desk of Nirmaya Multispeciality Hospital, Hinjewadi, Pune
(a fictional hospital). Two inbound calls from the same caller: the first books an appointment
and fills the cache; the second, next day, moves it, and lands on every match level and on every
refusal. Two editions share the structure: ``hi``, the Hinglish a Pune desk really speaks, and
``en``, Indian English. ``DEMO_LANG`` selects the edition (default ``hi``).

Three agent modes share this script:

- ``scripted``: the agent says exactly the turns below (a deterministic stand-in for the LLM).
- ``hybrid``: a real LLM with the persona, the patient records, and the pinned lines it must say
  verbatim when the situation arises (as real deployments pin their greeting and disclosure).
- ``pure``: the same LLM with the persona and records only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

HOSPITAL = "City General Hospital"
AGENT = "Ananya"
LANGUAGE = os.environ.get("DEMO_LANG", "hi")  # the cache scope and the edition
LANGUAGE_NAME = {"hi": "Hinglish", "en": "English"}

# Sarvam bulbul:v4-flash speakers per edition.
VOICES = {
    "hi": {"agent": "ishita_enhi_customer", "caller": "shubh_enhi_companion", "code": "hi-IN"},
    "en": {"agent": "ishita_enhi_customer", "caller": "shubh_enhi_companion", "code": "en-IN"},
}
AGENT_VOICE = VOICES[LANGUAGE]["agent"]
CALLER_VOICE = VOICES[LANGUAGE]["caller"]
TTS_LANGUAGE_CODE = VOICES[LANGUAGE]["code"]

# ---------------------------------------------------------------------------
# Patient records and doctor availability (fictional), shared by every mode.
# ---------------------------------------------------------------------------

RECORDS = {
    "9820012345": {"name": "Rohan Deshmukh", "patient_id": "CGH-2019-4471", "upcoming": None},
}

AVAILABILITY = {
    "Dr. Kulkarni (cardiology)": "Thursday 10:30 AM, Friday 4:00 PM. Friday morning is full.",
}

# ---------------------------------------------------------------------------
# Pinned lines: what the agent must say verbatim when the situation arises.
# ---------------------------------------------------------------------------

PINNED_BY_LANG = {
    "hi": {
        "greeting": f"{HOSPITAL} में call करने के लिए धन्यवाद, मैं {AGENT} बोल रही हूँ।",
        "disclosure": "यह call quality और training के लिए record की जा रही है।",
        "help": "बताइए, मैं आपकी क्या मदद कर सकती हूँ?",
        "ask_number": "कृपया अपना registered mobile number बताइए।",
        "record": "धन्यवाद, आपका record मिल गया, patient ID {last4} पर खत्म होता है।",
        "appointment": "{doctor} के साथ आपकी appointment {day} को {time} पर है।",
        "morning": "आप morning slot लेना चाहेंगे।",
        "confirmed": "मैंने आपकी appointment confirm कर दी है।",
        "instructions": "कृपया hospital पंद्रह minute पहले पहुँचें और एक photo ID साथ लाएँ।",
        "sms": "Appointment की details आपको थोड़ी देर में SMS से मिल जाएँगी।",
        "anything_else": "क्या मैं आपकी किसी और चीज़ में मदद कर सकती हूँ?",
        "closing": f"{HOSPITAL} में call करने के लिए धन्यवाद, अपना ख्याल रखिए।",
    },
    "en": {
        "greeting": f"Thank you for calling {HOSPITAL}, this is {AGENT} speaking.",
        "disclosure": "This call is being recorded for quality and training purposes.",
        "help": "How can I help you today?",
        "ask_number": "May I have your registered mobile number, please?",
        "record": "Thank you, I can see your record, patient ID ending in {last4}.",
        "appointment": "Your appointment with {doctor} is on {day} at {time}.",
        "morning": "You would like the morning slot.",
        "confirmed": "I have confirmed your appointment.",
        "instructions": "Please reach the hospital fifteen minutes early and carry a photo ID.",
        "sms": "You will receive an SMS with the appointment details shortly.",
        "anything_else": "Is there anything else I can help you with today?",
        "closing": f"Thank you for calling {HOSPITAL}, take care.",
    },
}
PINNED = PINNED_BY_LANG[LANGUAGE]


# ---------------------------------------------------------------------------
# The calls. A call is the caller's lines in order; the scripted agent answers each with a turn.
# Turn 0 is what the agent says on connect, before the caller speaks.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    """What the cache should do with one agent sentence in the second call."""

    outcome: str  # "hit" or "miss"
    level: str | None = None  # "A", "B", "C" for a hit
    short: str = ""  # the badge's reason, a word or two
    why: str = ""  # the full explanation


@dataclass(frozen=True)
class Turn:
    sentences: tuple[str, ...]
    expect: tuple[Expect | None, ...] = ()


@dataclass(frozen=True)
class Call:
    id: str
    language: str
    caller: str
    voice: str  # Sarvam speaker for the caller
    caller_lines: tuple[str, ...]
    agent_turns: tuple[Turn, ...]  # len(caller_lines) + 1: turn 0 on connect
    fills: bool  # True for the first call
    pause_redis_before_line: int | None = None  # 1-based caller line; Redis is paused for 5 s before it


def _t(*sentences: str, expect: tuple[Expect | None, ...] = ()) -> Turn:
    return Turn(tuple(sentences), expect or tuple(None for _ in sentences))


HIT_A = Expect("hit", "A", "", "Same sentence, same voice: played from the cache.")
HIT_B_FILLER = Expect("hit", "B", "filler", "A leading filler word. Same words, same clip: Level B.")
HIT_B_PUNCT = Expect("hit", "B", "punctuation", "Only the punctuation differs: Level B.")
HIT_C = Expect(
    "hit",
    "C",
    "paraphrase",
    "A paraphrase. The encoder proposed it, the vetoes passed, the judge agreed: Level C.",
)
MISS_ID = Expect("miss", None, "identifier", "Carries an identifier: never stored, so nothing to hit.")
MISS_SLOT = Expect("miss", None, "day and time changed", "A fact changed: the caller must hear the new one.")
MISS_QUESTION = Expect("miss", None, "question", "A question is never played from a statement's audio.")
MISS_NEGATION = Expect("miss", None, "negation", "A negation. Never served from a stored clip.")
MISS_NEW = Expect(
    "miss", None, "new", "A sentence the agent has never said. Provider, and stored for next time."
)

# Per edition: the caller's lines and the free (non-pinned) agent sentences, in order of use.
_TEXT = {
    "hi": {
        "c1": (
            "Hello, मुझे Dr. Kulkarni के साथ cardiology का follow-up appointment book करना है।",
            "मेरा number है nine eight two zero zero, one two three four five.",
            "हाँ, Thursday morning ठीक रहेगा।",
            "हाँ, confirm कर दीजिए।",
            "नहीं, बस इतना ही, thank you.",
        ),
        "c2": (
            "Hello, मेरी Dr. Kulkarni के साथ Thursday की appointment है, उसे मुझे Friday पर shift करना है।",
            "मेरा number है nine eight two zero zero, one two three four five.",
            "Friday morning हो सकता है क्या?",
            "ठीक है, वही कर दीजिए।",
            "नहीं, thank you.",
        ),
        "sure": "ज़रूर।",
        "available": "Dr. Kulkarni Thursday morning available हैं।",
        "slot_ok": "क्या वह slot आपके लिए ठीक रहेगा?",
        "confirm_q": "क्या मैं इसे confirm कर दूँ?",
        "which_day": "आप इसे किस दिन shift करना चाहेंगे?",
        "morning_q": "आप morning slot लेना चाहेंगे?",
        "not_available": "Dr. Kulkarni Friday morning available नहीं हैं।",
        "friday_pm": "वे Friday को 4:00 PM पर available हैं।",
        "confirmed_filler": "ठीक है, मैंने आपकी appointment confirm कर दी है।",
        "instructions_punct": "कृपया hospital पंद्रह minute पहले पहुँचें, और एक photo ID साथ लाएँ।",
        "sms_paraphrase": "आपको appointment की details कुछ ही देर में SMS पर मिल जाएँगी।",
    },
    "en": {
        "c1": (
            "Hi, I'd like to book a follow-up appointment with Dr. Kulkarni in cardiology.",
            "My number is nine eight two zero zero, one two three four five.",
            "Yes, Thursday morning works for me.",
            "Yes, please confirm it.",
            "No, that's all, thank you.",
        ),
        "c2": (
            "Hello, I have an appointment with Dr. Kulkarni on Thursday, and I'd like to move it to Friday.",
            "My number is nine eight two zero zero, one two three four five.",
            "Is Friday morning possible?",
            "Okay, go ahead with that.",
            "No, thank you.",
        ),
        "sure": "Certainly.",
        "available": "Dr. Kulkarni is available on Thursday morning.",
        "slot_ok": "Would that slot work for you?",
        "confirm_q": "Shall I confirm it?",
        "which_day": "Which day would you like to move it to?",
        "morning_q": "You would like the morning slot?",
        "not_available": "Dr. Kulkarni is not available on Friday morning.",
        "friday_pm": "He is available on Friday at 4:00 PM.",
        "confirmed_filler": "Okay, I have confirmed your appointment.",
        "instructions_punct": "Please reach the hospital fifteen minutes early, and carry a photo ID.",
        "sms_paraphrase": "The appointment details will be sent to you by SMS in a moment.",
    },
}

P = PINNED
X = _TEXT[LANGUAGE]

CALLS: tuple[Call, ...] = (
    Call(
        id="call-1",
        language=LANGUAGE,
        caller="Rohan Deshmukh",
        voice=CALLER_VOICE,
        fills=True,
        caller_lines=X["c1"],
        agent_turns=(
            _t(P["greeting"], P["disclosure"], P["help"]),
            _t(X["sure"], P["ask_number"]),
            _t(P["record"].format(last4="4471"), X["available"], X["slot_ok"]),
            _t(
                P["morning"],
                P["appointment"].format(doctor="Dr. Kulkarni", day="Thursday", time="10:30 AM"),
                X["confirm_q"],
            ),
            _t(P["confirmed"], P["instructions"], P["sms"], P["anything_else"]),
            _t(P["closing"]),
        ),
    ),
    Call(
        id="call-2",
        language=LANGUAGE,
        caller="Rohan Deshmukh",
        voice=CALLER_VOICE,
        fills=False,
        caller_lines=X["c2"],
        agent_turns=(
            _t(P["greeting"], P["disclosure"], P["help"], expect=(HIT_A, HIT_A, HIT_A)),
            _t(X["sure"], P["ask_number"], expect=(HIT_A, HIT_A)),
            _t(
                P["record"].format(last4="4471"),
                P["appointment"].format(doctor="Dr. Kulkarni", day="Thursday", time="10:30 AM"),
                X["which_day"],
                expect=(MISS_ID, HIT_A, MISS_NEW),
            ),
            _t(
                X["morning_q"],
                X["not_available"],
                X["friday_pm"],
                expect=(MISS_QUESTION, MISS_NEGATION, MISS_NEW),
            ),
            _t(
                X["confirmed_filler"],
                P["appointment"].format(doctor="Dr. Kulkarni", day="Friday", time="4:00 PM"),
                X["instructions_punct"],
                X["sms_paraphrase"],
                P["anything_else"],
                expect=(HIT_B_FILLER, MISS_SLOT, HIT_B_PUNCT, HIT_C, HIT_A),
            ),
            _t(P["closing"], expect=(HIT_A,)),
        ),
        pause_redis_before_line=5,
    ),
)

CALLS_BY_ID = {c.id: c for c in CALLS}


def filling_call(language: str = LANGUAGE) -> Call:
    return next(c for c in CALLS if c.language == language and c.fills)


# ---------------------------------------------------------------------------
# Prompts for the LLM modes.
# ---------------------------------------------------------------------------


def _records_block() -> str:
    lines = []
    for number, r in RECORDS.items():
        upcoming = r["upcoming"] or "none"
        lines.append(
            f"- Mobile {number}: {r['name']}, patient ID {r['patient_id']}, upcoming appointment: {upcoming}"
        )
    for doctor, slots in AVAILABILITY.items():
        lines.append(f"- {doctor}: available {slots}")
    return "\n".join(lines)


_STYLE = {
    "hi": (
        "You speak the natural Hinglish of a Pune hospital desk: Hindi sentences in Devanagari with the English words "
        "such callers use (appointment, slot, confirm, record, SMS, doctor names, days, times) kept in English. "
        "End each sentence with a Hindi full stop (।) or a question mark."
    ),
    "en": "You speak clear Indian English. End each sentence with a full stop or a question mark.",
}


def persona_prompt(language: str = LANGUAGE) -> str:
    return f"""You are {AGENT}, the appointment desk agent at {HOSPITAL}, Hinjewadi, Pune, on a phone call.
{_STYLE[language]} Your words are spoken aloud by text-to-speech, so write plain sentences: no lists, no markdown, no emojis, no abbreviations the voice would misread. Keep each reply to one to four short sentences, one idea per sentence.

Patient records and doctor availability (the caller gives a mobile number; look it up here):
{_records_block()}

When you read a patient ID aloud, say only its last four digits. Say times as "10:30 AM" and days by their English name. Be warm, brief and professional, as a real hospital desk is.
"""


def hybrid_prompt(language: str = LANGUAGE) -> str:
    lines = "\n".join(f"- {k}: {v}" for k, v in PINNED_BY_LANG[language].items())
    return (
        persona_prompt(language)
        + f"""
The hospital has standard lines. Whenever the situation arises, say the standard line word for word, with no change of punctuation or wording, then continue naturally. Fill the braces with the facts for this caller:
{lines}

The call opens with: greeting, disclosure, help, in that order, as three sentences.
To look up a caller you need the mobile number: ask with the ask_number line, then use the record line.
When you state an appointment, use the appointment line. When you confirm one, use the confirmed line, then the instructions line, then the sms line, then ask anything_else. When the caller is done, say the closing line and nothing after it.
"""
    )


def opening_instruction(language: str = LANGUAGE) -> str:
    return "The caller has just connected. Greet them: say the greeting, the disclosure and the help line."
