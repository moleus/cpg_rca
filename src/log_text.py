"""Text rules shared by static templates and runtime log matching."""

import re
import string

WILDCARD = "<*>"


def normalize_text(text: str) -> str:
    """
    Remove ALL escape sequences and special symbols EXCEPT hyphens (-).
    Keeps hyphens for compound words like "user-failed", "connection-timeout".
    """
    if not text:
        return ""

    text = str(text)

    text = text.replace(r"\\", " ")
    text = text.replace(r"\"", " ")
    text = text.replace(r"\'", " ")
    text = text.replace(r"\/", " ")
    text = re.sub(r"\[a-zA-Z]", " ", text)

    PUNCTUATION_TO_REMOVE = set(string.punctuation)
    PUNCTUATION_TO_REMOVE.discard("-")  # KEEP hyphens

    for p in PUNCTUATION_TO_REMOVE:
        text = text.replace(p, " ")

    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    text = text.lower()
    return text


def template_tokens(template: str) -> tuple[str, ...]:
    """Normalize static text and keep each wildcard as a separate token."""
    tokens = []
    for part in re.split(r"(<\*>)", template):
        if part == WILDCARD:
            tokens.append(WILDCARD)
        else:
            tokens.extend(normalize_text(part).split())
    return tuple(tokens)
