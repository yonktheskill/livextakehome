"""Local MVP checks, not semantic moderation or a prompt-injection guarantee.

Inspect normalized copies only; never rewrite an attendee's identity or an outgoing
booking field to make it pass. No services, model calls, or raw rejected-text logs.
"""

import re
import unicodedata

POLICY_VERSION = 2
SAFE_OUTPUT = "I couldn't display that response safely. Please rephrase your scheduling request."
WITHHELD = "[content withheld by safety checks]"
NOTICES = {
    "credentials": "Please remove credentials from chat. Use connection settings for Cal.com keys.",
    "withheld_details": "Some required details were withheld. Please supply them directly.",
}
CREDENTIALS = re.compile(
    r"\b(?:sk[-_](?:or[-_])?|cal_(?:live_|test_)?|gh[pousr]_|github_pat_|"
    r"AKIA|AIza|xox[baprs]-)[A-Za-z0-9_./+\-=]{8,}|"
    r"\bBearer\s+\S+|\bAuthorization\s*[:=]\s*Basic\s+\S+|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"\b(?:api[-_ ]?key|password|secret|access[-_ ]?token)[\"']?\s*[:=]\s*\S+",
    re.I,
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
    # User scheduling data is not rejected by word lists or injection keywords.
    # Professional output and untrusted-data boundaries are model policy; actual
    # write authorization must be grounded in original user messages in code.
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
