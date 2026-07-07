"""Never-persist secrets filter (spec §Autonomy & secrets, north-star §6.1).

EDITH ingests the owner's CLAUDE.md, which contains LIVE credentials. The
never-persist filter runs FIRST in remember: secret-shaped material is stripped
before anything is written to the graph/vector store. Only sanitized facts land.
"""

from edith.memory.secrets import contains_secret, sanitize_text


def test_detects_common_secret_shapes():
    assert contains_secret("client_secret: GOCSPX-EXAMPLE_FAKE_SECRET_DO_NOT_STORE")
    assert contains_secret("BIFROST_API_KEY=sk-proj-abc123def456ghi789")
    assert contains_secret("refresh_token: 1//0gFAKErefreshTokenValueThatIsLong")
    assert contains_secret("-----BEGIN PRIVATE KEY-----")
    assert contains_secret("password = hunter2hunter2")


def test_leaves_ordinary_facts_untouched():
    fact = "the onboarding-portal Unknown object error was the service account not shared"
    assert not contains_secret(fact)
    assert sanitize_text(fact) == fact


def test_sanitize_redacts_the_secret_value_not_the_fact():
    line = "owner has client_secret: GOCSPX-EXAMPLE_FAKE_SECRET_DO_NOT_STORE configured"
    out = sanitize_text(line)
    assert "GOCSPX-EXAMPLE_FAKE_SECRET_DO_NOT_STORE" not in out
    assert "[REDACTED]" in out


def test_markdown_wrapped_assignment_redacts_the_value_not_the_wrapper():
    """A markdown-bold `**key:** value` must redact the VALUE, not just the `**`.

    Regression: `[:=]\\s*(\\S+)` used to capture the `**` right after the colon,
    leaving the real value in the clear. Uses only FAKE tokens.
    """
    line = "- **refresh_token:** 1//0gFAKErefreshTokenValueThatIsLong"
    out = sanitize_text(line)
    assert "1//0gFAKErefreshTokenValueThatIsLong" not in out
    assert "[REDACTED]" in out


def test_google_refresh_token_prefix_is_a_shape_backstop():
    assert contains_secret("1//0gFAKErefreshTokenValueThatIsLong")
    assert "1//0gFAKErefreshTokenValueThatIsLong" not in sanitize_text(
        "some prose 1//0gFAKErefreshTokenValueThatIsLong more prose"
    )
