"""Per-call conversation language. The Realtime model does not choose it.

One CallLanguageState on the sideband session is the only controller:
transcript → detect → lock or confirm a switch → session instructions → reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Appended last so it overrides any earlier "detect / match / switch" wording.
LANGUAGE_CONTROL_MARKER = "CONVERSATION LANGUAGE (APPLICATION CONTROLLED)"

_URDU_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")

# Acknowledgements. Alone, or as the whole utterance, they are not a language.
_FILLER_TOKENS = frozenset(
    {
        "okay",
        "ok",
        "yes",
        "no",
        "thanks",
        "thank",
        "you",
        "hello",
        "hi",
        "hey",
        "acha",
        "achha",
        "theek",
        "thik",
        "han",
        "haan",
        "ji",
        "jee",
        "nahi",
        "nahin",
        "hmm",
        "hm",
        "sure",
        "alright",
        "fine",
        "bye",
        "goodbye",
        "shukriya",
        "shukria",
        "yeah",
        "yep",
        "nope",
    }
)
_FILLER_PHRASES = frozenset(
    {
        "thank you",
        "theek hai",
        "thik hai",
        "acha theek hai",
        "achha theek hai",
        "ok thanks",
        "okay thanks",
    }
)

# Roman Urdu function words. English loanwords (service, appointment) are omitted
# so they do not vote for English inside an Urdu sentence.
_URDU_TOKENS = frozenset(
    {
        "mujhe",
        "mujhay",
        "mjhe",
        "aap",
        "aapki",
        "aapka",
        "apki",
        "apka",
        "kaise",
        "kaisay",
        "kese",
        "hain",
        "hai",
        "hoon",
        "hun",
        "ho",
        "ke",
        "ki",
        "ka",
        "kay",
        "mein",
        "mai",
        "main",
        "bare",
        "baare",
        "baray",
        "batain",
        "batao",
        "bataein",
        "batayein",
        "bataiye",
        "bata",
        "dein",
        "dijiye",
        "chahiye",
        "chahie",
        "kya",
        "kyun",
        "kyon",
        "kahan",
        "kitna",
        "kitne",
        "kitni",
        "ghar",
        "madad",
        "zaroor",
        "bilkul",
        "kijiye",
        "karain",
        "karein",
        "karen",
        "karta",
        "karti",
        "karte",
        "mera",
        "meri",
        "mere",
        "hamara",
        "hamari",
        "hamare",
        "aur",
        "yeh",
        "ye",
        "woh",
        "wo",
        "se",
        "ko",
        "bhi",
        "tarah",
        "baat",
        "raha",
        "rahi",
        "rahe",
        "assalamualaikum",
        "assalam",
        "alaikum",
    }
)

_ENGLISH_TOKENS = frozenset(
    {
        "i",
        "im",
        "can",
        "could",
        "would",
        "should",
        "please",
        "explain",
        "your",
        "you",
        "what",
        "where",
        "when",
        "which",
        "how",
        "need",
        "want",
        "looking",
        "house",
        "rent",
        "budget",
        "tell",
        "about",
        "the",
        "an",
        "our",
        "for",
        "with",
        "this",
        "that",
        "help",
        "price",
        "pricing",
        "monthly",
        "from",
        "my",
        "me",
        "is",
        "are",
        "am",
        "in",
        "of",
        "and",
        "a",
        "english",
        "now",
    }
)

_SWITCH_CONFIRMATIONS = 2


@dataclass
class CallLanguageState:
    """Single source of truth for one live call. None until a real preference or detection."""

    call_language: str | None = None
    language_locked: bool = False
    language_switch_candidate: str | None = None
    language_switch_count: int = 0


def preference_to_call_language(raw: str | None) -> str | None:
    """Map an explicit preference to 'en' or 'ur'.

    ``roman_urdu`` is the CRM default for new contacts, not a confirmed preference.
    """
    if raw is None:
        return None
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    if key in {"en", "english", "eng"}:
        return "en"
    if key in {"ur", "urdu", "pakistani_urdu"}:
        return "ur"
    return None


def state_from_preference(raw: str | None) -> CallLanguageState:
    lang = preference_to_call_language(raw)
    if lang is None:
        return CallLanguageState()
    return CallLanguageState(call_language=lang, language_locked=True)


def detect_caller_language(text: str) -> str | None:
    """Return 'en' or 'ur' for a meaningful utterance, else None.

    None means ignore: filler, or not enough evidence. Transcription is used
    as-is. This does not translate.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if _URDU_SCRIPT.search(raw):
        return "ur"
    normalized = raw.lower().replace("\u2019", "").replace("'", "")
    normalized = re.sub(r"[^a-z\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or normalized in _FILLER_PHRASES:
        return None
    tokens = normalized.split(" ")
    if all(token in _FILLER_TOKENS for token in tokens):
        return None
    content = [token for token in tokens if token not in _FILLER_TOKENS]
    if not content:
        return None
    urdu_hits = sum(1 for token in content if token in _URDU_TOKENS)
    english_hits = sum(1 for token in content if token in _ENGLISH_TOKENS)
    if urdu_hits >= 2 and urdu_hits > english_hits:
        return "ur"
    if english_hits >= 2 and english_hits > urdu_hits:
        return "en"
    return None


def observe_caller_transcript(state: CallLanguageState, text: str) -> list[str]:
    """Update call language. Returns log lines. Does not log the transcript."""
    detected = detect_caller_language(text)
    if detected is None:
        return []
    logs = [f"LANGUAGE_DETECTED candidate={detected}"]
    if not state.language_locked or state.call_language is None:
        state.call_language = detected
        state.language_locked = True
        state.language_switch_candidate = None
        state.language_switch_count = 0
        logs.append(f"LANGUAGE_LOCKED language={detected}")
        return logs
    if detected == state.call_language:
        state.language_switch_candidate = None
        state.language_switch_count = 0
        return logs
    if state.language_switch_candidate != detected:
        state.language_switch_candidate = detected
        state.language_switch_count = 1
    else:
        state.language_switch_count += 1
    logs.append(
        f"LANGUAGE_SWITCH_CANDIDATE {detected} count={state.language_switch_count}"
    )
    if state.language_switch_count >= _SWITCH_CONFIRMATIONS:
        state.call_language = detected
        state.language_locked = True
        state.language_switch_candidate = None
        state.language_switch_count = 0
        logs.append(f"LANGUAGE_SWITCH_CONFIRMED language={detected}")
    return logs


def language_control_block(state: CallLanguageState) -> str:
    lines = [
        LANGUAGE_CONTROL_MARKER,
        "Conversation language is controlled by the application. Never choose or change the conversation language yourself.",
        "This block overrides every earlier instruction about detecting, matching, mixing, or switching language.",
    ]
    if state.call_language == "en":
        lines.extend(
            [
                'call_language: "en"',
                "Respond only in natural English.",
                "Do not answer in Urdu.",
                "Do not use Roman Urdu.",
                "Do not translate the response into Urdu.",
            ]
        )
    elif state.call_language == "ur":
        lines.extend(
            [
                'call_language: "ur"',
                "Respond in natural Pakistani Urdu.",
                "Do not switch to full English responses.",
                "English technical words, company names, product names, numbers and unavoidable terminology are allowed.",
                "Keep the main sentence structure Urdu.",
            ]
        )
    else:
        lines.extend(
            [
                "call_language: unknown",
                "Do not choose English or Urdu.",
                "The bilingual greeting, if already spoken, must not be repeated.",
                "Wait for the application to lock the language.",
            ]
        )
    return "\n".join(lines)


def apply_language_control(instructions: str, state: CallLanguageState) -> str:
    base = instructions or ""
    marker_at = base.find(LANGUAGE_CONTROL_MARKER)
    if marker_at != -1:
        base = base[:marker_at]
    return base.rstrip() + "\n\n" + language_control_block(state)
