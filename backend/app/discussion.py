import re
from collections.abc import Sequence
from dataclasses import dataclass


_QUESTION_START = (
    r"(?:how|what|which|who|why|when|where|can|could|would|will|do|does|did|"
    r"should|is|are|tell|explain|share|describe|please)\b"
)
_NAMES = r"players?\s+[abc]\b(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*)(?:players?\s+)?[abc]\b)*"
_NAMED_ADDRESS = re.compile(
    rf"(?:^\s*(?:(?:hey|hi)\s*,?\s+)?|,\s*)({_NAMES})(?=\s*(?:[,:?!.]|$|{_QUESTION_START}))"
    rf"|(?:^\s*(?:can|could|would|will)\s+|\b(?:question for|hear from)\s+)({_NAMES})(?!['’])",
    re.IGNORECASE,
)
_PRESENCE_QUESTION = re.compile(
    rf"\s*(?:where(?:\s+(?:is|are)|['’]s)\s+({_NAMES})(?:\s+now)?"
    rf"|(?:is|are)\s+({_NAMES})\s+(?:still\s+)?(?:here|there|around|online))\s*[?!.]*\s*",
    re.IGNORECASE,
)
_GROUP_REQUEST = re.compile(
    r"\b(?:both of you|you both|you all|each of you)\b"
    r"|^\s*(?:both players|everyone|everybody)\s*[:,]"
    r"|\b(?:can|could|would|will)\s+(?:both players|everyone|everybody)\b",
    re.IGNORECASE,
)
_SLOT = re.compile(r"\b[abc]\b", re.IGNORECASE)


@dataclass(frozen=True)
class ReplyTurn:
    slot: str
    required: bool
    direct: bool = False


def presence_addressees(text: str) -> set[str]:
    match = _PRESENCE_QUESTION.fullmatch(text)
    return {name.upper() for name in _SLOT.findall(match[1] or match[2])} if match else set()


def direct_addressees(text: str) -> set[str]:
    """Distinguish a direct address from simply mentioning another player."""
    names = {
        name.upper()
        for match in _NAMED_ADDRESS.finditer(text)
        for name in _SLOT.findall(match[1] or match[2])
    }
    names.update(presence_addressees(text))
    return names


def reply_turns(text: str, ai_slots: Sequence[str]) -> tuple[ReplyTurn, ...]:
    """Keep one conversational partner unless a message invites multiple responses."""
    names = direct_addressees(text)
    if names:
        return tuple(ReplyTurn(slot, required=True, direct=True) for slot in ai_slots if slot in names)
    responders = ai_slots if _GROUP_REQUEST.search(text) else ai_slots[:1]
    return tuple(ReplyTurn(slot, required=index == 0) for index, slot in enumerate(responders))
