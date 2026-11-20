import re
from collections.abc import Sequence
from difflib import SequenceMatcher


_DISPLAY_LABEL = re.compile(r"\b(?i:player|candidate)\s+([ABCbc]|a(?=\s*(?:[.,!?:;\"'”’)]|$)))\b")
_UNKNOWN_LABEL = re.compile(r"\b(?:Player|Candidate)\s+[D-HJ-Z]\b")
_PLAYER = r"\b(?:player|candidate)\s+[abc]\b"
_OPTION = rf"(?:[\"'“‘]\s*)?(?:{_PLAYER}|(?-i:\b[ABC]\b)|\b(?:you|myself)\b)(?!['’]s\b)[\"'”’]?"
# Match the voting act separately from its first-person subject. A bounded
# same-clause bridge accepts intervening planning verbs and adverbs without
# maintaining an ever-growing list of phrases such as "will proceed to".
_FIRST_PERSON = re.compile(r"\bi(?:['’](?:ll|d|m|ve))?\b", re.IGNORECASE)
_VOTING_ACT = re.compile(
    rf"\bvot(?:e|ed|ing)\b(?:\s+(?:for\s+)?{_OPTION}|[^.!?;\n]{{0,80}}?\bfor\s+{_OPTION})"
    rf"|\b(?:cast|casting)\s+(?:my|a)\s+(?:vote|ballot)\b[^.!?;\n]{{0,80}}?\bfor\s+{_OPTION}"
    rf"|\b(?:give|gave|giving)\s+my\s+vote\s+to\s+{_OPTION}"
    rf"|\b(?:pick|picked|picking|choose|chose|choosing|select|selected|selecting|back|backed|backing|prefer|preferred|preferring|support|supported|supporting)\s+{_OPTION}"
    rf"|\b(?:lean(?:ing)?\s+towards?|go(?:ing)?\s+with)\s+{_OPTION}",
    re.IGNORECASE,
)
_OTHER_ACTOR_OR_QUESTION = re.compile(
    r"\b(?:you|they|he|she|player|candidate|ask(?:ed|ing)?|wonder(?:ed|ing)?|whether|why|how|who)\b",
    re.IGNORECASE,
)
_ABILITY = re.compile(
    r"^i\s+(?:can|may)\s*$|^i(?:['’]m|\s+am)\s+(?:allowed|eligible|permitted)\s+to\s*$",
    re.IGNORECASE,
)
_TEMPORARY_LIMIT = re.compile(r"\b(?:yet|until|before\s+voting|during\s+discussion|right\s+now)\b", re.IGNORECASE)
_BALLOT_LIMIT = re.compile(
    r"\b(?:twice|more\s+than\s+once|(?:a\s+second|another)\s+(?:vote|ballot))\b",
    re.IGNORECASE,
)
_CHOICE = re.compile(
    rf"\bi(?:\s+|['’](?:ll|d|m|ve)\s+)"
    rf"(?:(?:will|would|am|have|already|definitely|probably|currently|still|going to|plan to|intend to|"
    rf"want to|like to|love to|hope to|expect to|wish to|decided to|think i(?:['’]ll| will| would)?)\s+)*"
    rf"(?:pick|picked|choose|chose|select|selected|back|prefer|support)\b\s+{_OPTION}"
    rf"|\bmy\s+(?:(?:vote|ballot)\s+(?:is(?:\s+going)?|goes|went|will go|would go)\s+"
    rf"(?:to|for)|(?:pick|choice|preferred (?:player|candidate))\s+(?:is|would be))\s+{_OPTION}"
    rf"|{_OPTION}\s+(?:(?:has|gets|got|would get)\s+my\s+(?:vote|ballot)|is\s+my\s+(?:pick|choice))\b"
    rf"|\b(?:count me in for|i(?:['’]m| am)\s+(?:leaning towards?|(?:going )?with))\s+{_OPTION}"
    rf"|^\s*(?:my\s+)?(?:vote|ballot|pick|choice)\s*:\s*{_OPTION}",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"[^.!?\n]+[.!?]*")
_QUESTION = re.compile(
    rf"\s*(?:{_PLAYER}\s*,\s*)?(?:how|who|what|which|when|why|if|"
    rf"can|could|would|will|should|do|does|did|is|am|are)\b",
    re.IGNORECASE,
)
_REPORTED_SPEECH = re.compile(
    rf"\b(?:{_PLAYER}|you|they|someone)\s+(?:said|wrote|asked|posted|mentioned|told me)"
    r"(?:\s+that)?\s*[, :]*\s*(?:\"[^\"\n]+\"|“[^”\n]+”)",
    re.IGNORECASE,
)
_VOTE_INSTRUCTION = re.compile(
    rf"^\s*(?:please\s+)?(?:vote\s+for|pick|choose|select|back|support)\s+{_OPTION}",
    re.IGNORECASE,
)
_INELIGIBLE = re.compile(
    r"\b(?:not|never|no longer)\s+(?:eligible|allowed|permitted|entitled)\s+"
    r"(?:to\s+)?(?:vote\b|cast\s+(?:a\s+|my\s+)?(?:vote|ballot)\b|be\s+(?:the\s+|a\s+)?Decision\s+Maker\b)"
    r"|\b(?:self[- ]vot(?:e|es|ing)|voting\s+for\s+(?:myself|yourself|ourselves|themselves))\s+"
    r"(?:(?:is|are)\s+)?(?:forbidden|prohibited|not\s+allowed|not\s+permitted|banned)\b"
    r"|\b(?:self[- ]vot(?:e|es|ing)|voting\s+for\s+(?:myself|yourself|ourselves|themselves))\s+"
    r"(?:isn['’]t|aren['’]t|is\s+not|are\s+not)\s+(?:allowed|permitted|eligible)\b"
    r"|\b(?:i\s+(?:cannot|can['’]t|may\s+not)|i(?:['’]m|\s+am)\s+unable\s+to)\s+"
    r"(?:vote|cast\s+(?:a\s+|my\s+)?(?:vote|ballot))\s+for\s+myself\b",
    re.IGNORECASE,
)
_LEADING_ADDRESS = re.compile(r"^\s*(?:player|candidate)\s+[abc]\s*[:,]\s*", re.IGNORECASE)
_WORD = re.compile(r"\w+", re.UNICODE)
_QUESTIONS = re.compile(r"(?:^|[.!?])\s*([^.!?]+\?)")


def normalize_player_labels(text: str) -> str:
    """Use the labels displayed in the browser for AI chat."""
    return _DISPLAY_LABEL.sub(lambda match: f"Player {match[1].upper()}", text)


def has_unknown_player_label(text: str) -> bool:
    return bool(_UNKNOWN_LABEL.search(text))


def has_self_narration(text: str, slot: str) -> bool:
    """Catch an own-label subject instead of speaking as that player.

    A quoted/reported question, a vocative, or a statement about another player
    is not a self-description. This is a voice check, not a factuality check.
    """
    slot = slot.upper()
    if slot not in {"A", "B", "C"}:
        return False
    return bool(re.match(
        rf"^\s*Player\s+{slot}\b(?:['’]s\b|\s+(?=\w))",
        normalize_player_labels(text), re.IGNORECASE,
    ))


def _comparison_words(text: str) -> list[str]:
    return _WORD.findall(_LEADING_ADDRESS.sub("", normalize_player_labels(text)).casefold())


def is_near_duplicate(text: str, previous: Sequence[str]) -> bool:
    """Detect substantial near-verbatim repetition, preserving new contributions.

    This deliberately does not infer semantic equivalence. A new question or
    several new content words can make a related reply worth posting.
    """
    words = _comparison_words(text)
    if len(words) < 9:
        return False
    vocabulary = set(words)
    questions = [_comparison_words(match[1]) for match in _QUESTIONS.finditer(text)]
    for earlier in previous:
        prior = _comparison_words(earlier)
        if len(prior) < 9:
            continue
        prior_vocabulary = set(prior)
        added = vocabulary - prior_vocabulary
        if len(added) >= 4:
            continue
        # A short, genuinely new question is a conversational action even when
        # it follows a familiar point. Merely changing punctuation is not.
        if any(len(set(question) - prior_vocabulary) >= 2 for question in questions):
            continue
        containment = len(vocabulary & prior_vocabulary) / min(len(vocabulary), len(prior_vocabulary))
        if containment >= 0.9 and SequenceMatcher(None, prior, words, autojunk=False).ratio() >= 0.85:
            return True
    return False


def discloses_vote(text: str) -> bool:
    """Recognize public ballot choices and instructions to vote for a named player."""
    dialogue = _REPORTED_SPEECH.sub("", text)
    for sentence in _SENTENCES.findall(dialogue):
        if sentence.endswith("?") and _QUESTION.match(sentence):
            continue
        if _own_voting_act(sentence) or _CHOICE.search(sentence) or _VOTE_INSTRUCTION.search(sentence):
            return True
    return False


def _own_voting_act(sentence: str) -> bool:
    for act in _VOTING_ACT.finditer(sentence):
        before = sentence[:act.start()]
        for subject in reversed(list(_FIRST_PERSON.finditer(before))):
            bridge = before[subject.end():]
            if re.search(r"[.!?;:\n]", bridge) or len(_WORD.findall(bridge)) > 12:
                continue
            # An embedded question about someone else's choice is not the
            # speaker's ballot. Include its framing before an embedded "I".
            clause = re.split(r"[;:]|,\s*|\b(?:but|so)\b", before, flags=re.IGNORECASE)[-1]
            if _OTHER_ACTOR_OR_QUESTION.search(bridge) or re.search(
                r"\b(?:ask(?:ed|ing)?|wonder(?:ed|ing)?|whether)\b", clause, re.IGNORECASE,
            ):
                continue
            own_prefix = before[subject.start():].strip()
            if _ABILITY.fullmatch(own_prefix):
                continue
            if (_TEMPORARY_LIMIT.search(sentence) or _BALLOT_LIMIT.search(sentence)) and re.search(
                r"\b(?:cannot|can['’]t|unable|not\s+allowed)\b", own_prefix, re.IGNORECASE,
            ):
                continue
            return True
    return False


def public_chat_violation(text: str) -> str | None:
    """Validate AI public chat without changing the private voting rules.

    Every assigned player is eligible and may self-vote. Privacy constrains
    announcing a ballot, not which candidate a player may privately choose.
    Callers apply this only to generated AI text, never to human dialogue.
    """
    dialogue = _REPORTED_SPEECH.sub("", text)
    for sentence in _SENTENCES.findall(dialogue):
        if sentence.endswith("?") and _QUESTION.match(sentence):
            continue
        if _INELIGIBLE.search(sentence) and not (
            _TEMPORARY_LIMIT.search(sentence) or _BALLOT_LIMIT.search(sentence)
        ):
            return "incorrect_eligibility"
    return "ballot_disclosure" if discloses_vote(text) else None
