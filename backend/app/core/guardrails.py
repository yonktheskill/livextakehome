"""Local MVP checks, not semantic moderation or a prompt-injection guarantee.

Inspect normalized copies only; never rewrite an attendee's identity or an outgoing
booking field to make it pass. No services, model calls, or raw rejected-text logs.
"""

import re
import unicodedata
from pathlib import Path

POLICY_VERSION = 1
SAFE_OUTPUT = "I couldn't display that response safely. Please rephrase your scheduling request."
WITHHELD = "[content withheld by safety checks]"
NOTICES = {
    "profanity": "Please rephrase your message without profanity or abusive language.",
    "credentials": "Please remove credentials from chat. Use connection settings for Cal.com keys.",
    "instruction_override": "Please send a scheduling request that follows the app's rules.",
    "withheld_details": "Some required details were withheld. Please supply them directly.",
}
TERMS = [
    line.strip()
    for line in Path(__file__).with_name("profanity.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
]
PROFANITY = re.compile(
    r"(?<!\w)(?:" + "|".join(r"[\W_]*".join(map(re.escape, term)) for term in TERMS) + r")(?!\w)"
)
MASKED = re.compile(r"(?<!\w)(?:f\*+ck(?:ing|ed|er|ers|s)?|sh\*+t(?:ty|s)?|b\*+tch)(?!\w)")
LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"}
)
CREDENTIALS = re.compile(
    r"\b(?:sk[-_](?:or[-_])?|cal_(?:live_|test_)?|gh[pousr]_|github_pat_|"
    r"AKIA|AIza|xox[baprs]-)[A-Za-z0-9_./+\-=]{8,}|"
    r"\bBearer\s+\S+|\bAuthorization\s*[:=]\s*Basic\s+\S+|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"\b(?:api[-_ ]?key|password|secret|access[-_ ]?token)[\"']?\s*[:=]\s*\S+",
    re.I,
)
OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|override|bypass)\b.{0,60}\b"
    r"(?:instructions|system\s+prompt|safety\s+(?:rules|filters)|guardrails)\b|"
    r"\b(?:reveal|print|show|repeat|output)\b.{0,50}\b"
    r"(?:system\s+(?:prompt|instructions)|developer\s+(?:prompt|instructions))\b|"
    r"\b(?:you\s+are\s+now|act\s+as)\s+(?:an?\s+)?(?:unrestricted|unfiltered)\b|"
    r"<\s*/?\s*(?:system|developer)\s*>|\[\s*inst\s*\]",
    re.I | re.S,
)
SENSITIVE_FIELDS = {
    "apikey",
    "password",
    "secret",
    "authorization",
    "cookie",
    "accesstoken",
    "refreshtoken",
    "credentials",
    "credential",
    "token",
}


class ContentRejected(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(NOTICES[reason])


def normalized(text):
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text)).casefold()
    # Combining marks and invisible format characters can split prohibited terms.
    return "".join(c for c in text if unicodedata.category(c) not in {"Cf", "Mn"})


def reason(text, *, injection=True, secrets=()):
    copy = normalized(text)
    if (
        any(secret and (secret in text or normalized(secret) in copy) for secret in secrets)
        or CREDENTIALS.search(text)
        or CREDENTIALS.search(copy)
    ):
        return "credentials"
    if PROFANITY.search(copy) or PROFANITY.search(copy.translate(LEET)) or MASKED.search(copy):
        return "profanity"
    if injection and OVERRIDE.search(copy):
        return "instruction_override"
    return None


def require_content(value, *, secrets=()):
    if isinstance(value, str):
        if code := reason(value, secrets=secrets):
            raise ContentRejected(code)
    elif isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[^a-z]", "", str(key).lower()) in SENSITIVE_FIELDS:
                raise ContentRejected("credentials")
            require_content(str(key), secrets=secrets)
            require_content(item, secrets=secrets)
    elif isinstance(value, (list, tuple)):
        for item in value:
            require_content(item, secrets=secrets)


def safe_text(text, *, secrets=()):
    return SAFE_OUTPUT if reason(text, secrets=secrets) else text


def safe_data(value, *, secrets=()):
    """A display/context copy; canonical action evidence is never rewritten."""
    if isinstance(value, str):
        return WITHHELD if reason(value, secrets=secrets) else value
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if reason(str(key), secrets=secrets):
                result[WITHHELD] = WITHHELD
            elif re.sub(r"[^a-z]", "", str(key).lower()) in SENSITIVE_FIELDS:
                result[key] = WITHHELD
            else:
                result[key] = safe_data(item, secrets=secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [safe_data(item, secrets=secrets) for item in value]
    return value
