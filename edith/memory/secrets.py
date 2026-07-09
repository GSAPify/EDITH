"""Never-persist secrets filter (north-star §6.1, spec §Autonomy & secrets).

EDITH ingests the owner's CLAUDE.md, which holds LIVE credentials. This filter
runs FIRST in ``remember`` so a credential is stripped before anything reaches
the graph, the vector store, logs, or the bus. It stores the *fact of it*,
never the secret itself — matching the spec's example.

Deliberately conservative: matches labelled secret assignments (``key = value``,
``secret: value``), long token-shaped runs, and PEM headers. False positives
(over-redaction) are the safe failure here; a leaked credential is not.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# key/secret/token/password assignments: `name: value` or `name = value`.
# The value capture first skips any markdown/quote wrapper punctuation
# (``**``, backticks, quotes) between the delimiter and the value, so a
# ``- **refresh_token:** <value>`` line redacts the VALUE, not just the ``**``.
_ASSIGNMENT = re.compile(
    r"(?i)\b([\w.-]*(?:secret|token|password|passwd|api[_-]?key|client[_-]?secret|"
    r"private[_-]?key|access[_-]?key|refresh[_-]?token))\b\s*[:=]\s*[*`'\"]*\s*(\S+)"
)

# PEM / private-key block headers.
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# Credentials embedded in a connection URI: ``scheme://user:PASSWORD@host``. This
# is the spec-04 killer-demo leak — a pasted DB error carrying a Postgres/Redis/
# Mongo/AMQP URI with an inline password. Redact only the password group; the
# scheme, user, and host are not secret and keep the fact legible. The password
# run stops at the first ``@`` (real URIs percent-encode a literal ``@``).
_CONN_URI = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s@/]+)@")

# Provider-shaped tokens. Two families:
#  1. PREFIXED tokens — a known prefix then the rest of the run. Covers Google
#     client secret (GOCSPX-), OpenAI/Stripe/ElevenLabs (sk- / sk_), GitHub PATs
#     (ghp_ / github_pat_), and Slack (xoxb-/xoxp-/…). ``\b`` guards the prefix so
#     ``disk_usage`` / ``task-force`` don't trip ``sk``.
#  2. FIXED-shape standalone tokens that carry no ``key =`` label but can ride
#     inside a Brain response into spoken text (→ ElevenLabs, a third-party
#     cloud): AWS access-key ids (AKIA/ASIA + 16), Google API keys (AIza + run),
#     and Google's OAuth refresh token (1//…).
_TOKEN_PREFIX = re.compile(
    r"\b(?:GOCSPX-|sk[-_]|ghp_|github_pat_|xox[baprs]-)\S+"
    r"|1//[A-Za-z0-9_-]{10,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{20,}"
)


def contains_secret(text: str) -> bool:
    """True if ``text`` carries secret-shaped material."""
    return bool(
        _ASSIGNMENT.search(text)
        or _PEM.search(text)
        or _TOKEN_PREFIX.search(text)
        or _CONN_URI.search(text)
    )


def sanitize_text(text: str) -> str:
    """Return ``text`` with any secret value replaced by ``[REDACTED]``.

    The surrounding non-secret prose is preserved so the *fact* survives.
    """
    out = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}: {_REDACTED}", text)
    out = _PEM.sub(_REDACTED, out)
    out = _TOKEN_PREFIX.sub(_REDACTED, out)
    out = _CONN_URI.sub(lambda m: f"{m.group(1)}:{_REDACTED}@", out)
    return out
