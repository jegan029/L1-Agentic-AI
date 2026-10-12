"""Redaction before a provider request (ADR 0009). The corpus test is in tests/evals."""

from __future__ import annotations

import pytest

from src.l1_agent.ai.gateway import compose, section
from src.l1_agent.ai.redaction import EMAIL, PERSONAL, SECRET, Redactor


class TestWhatIsReplaced:
    @pytest.mark.parametrize(
        "text, gone",
        [
            ("password=Hunter2!x", "Hunter2!x"),
            ('{"api_key": "abcd1234efgh"}', "abcd1234efgh"),
            ("the password is Zx81-Passw0rd today", "Zx81-Passw0rd"),
            ("Authorization: Bearer abcDEF123456ghi", "abcDEF123456ghi"),
            ("https://user:p4ss@host.example/path", "user:p4ss"),
            ("AKIAABCDEFGHIJKLMNOP in the log", "AKIAABCDEFGHIJKLMNOP"),
            ("mail jane.doe@example.com now", "jane.doe@example.com"),
            ("call +44 20 7946 0958 please", "+44 20 7946 0958"),
            ("card 4111 1111 1111 1111 declined", "4111 1111 1111 1111"),
            ("card 4111-1111-1111-1111 declined", "4111-1111-1111-1111"),
            ("to DE89370400440532013000 URGENT today", "DE89370400440532013000"),
            ("SSN 078-05-1120 on file", "078-05-1120"),
            ("client 10.20.30.44 dropped", "10.20.30.44"),
            ("id 9f86d081884c7d659a2feaa0c55ad015 reused", "9f86d081884c7d659a2feaa0c55ad015"),
            ("value Qw7rT9yUi2oP4aS6dF8gH1jK3lZ5xC leaked", "Qw7rT9yUi2oP4aS6dF8gH1jK3lZ5xC"),
        ],
    )
    def test_the_value_is_gone_and_the_rest_is_kept(self, text, gone):
        out = Redactor().redact(text)
        assert gone not in out and "[[" in out

    def test_the_words_around_a_value_are_kept(self):
        out = Redactor().redact("the password=Hunter2!x was reset by jane.doe@example.com today")
        assert out == "the password=[[SECRET_1]] was reset by [[EMAIL_1]] today"

    def test_a_private_key_is_replaced_whole_even_when_cut_short(self):
        out = Redactor().redact("-----BEGIN RSA PRIVATE KEY-----\nMIIabc123\nmore")
        assert "MIIabc123" not in out and "more" not in out

    def test_the_callers_name_is_replaced_wherever_it_appears(self):
        redactor = Redactor(["Alexandra Papadopoulos", "", "Al"])
        out = redactor.redact("alexandra papadopoulos called. Ask Alexandra Papadopoulos.")
        assert "apadopoulos" not in out.lower()
        assert "Al" in redactor.redact("Also check"), "a short name is not searched for"


class TestWhatIsLeftAlone:
    @pytest.mark.parametrize(
        "text",
        [
            "Autosys job NIGHTLY_SETTLEMENT_LOAD_FINAL_STEP_RERUN failed with exit code 1",
            "queue PAYMENT.REQUEST on queue manager QMPROD01 depth 15000",
            "host app-server-prod01.internal.example at 2026-04-14 09:10:00",
            "INC0012345 opened; SOP-MQ-001 v3 matched with confidence 0.92",
            "index=app_logs sourcetype=log4j host=app-server-* (ERROR OR FATAL)",
            "order 1234 5678 9012 3456 is not a card number",
            "version 300.400.500.600 of the agent",
        ],
    )
    def test_operational_text_is_unchanged(self, text):
        assert Redactor().redact(text) == text

    def test_empty_and_none(self):
        assert Redactor().redact("") == "" and Redactor().redact(None) == ""


class TestPlaceholders:
    def test_the_same_value_gets_the_same_placeholder_for_the_whole_run(self):
        redactor = Redactor()
        first = redactor.redact("from a@example.com")
        second = redactor.redact("again a@example.com and b@example.com")
        assert first == "from [[EMAIL_1]]"
        assert second == "again [[EMAIL_1]] and [[EMAIL_2]]"
        assert redactor.found == {EMAIL: 2}

    def test_personal_data_is_put_back_and_a_secret_is_not(self):
        redactor = Redactor()
        sent = redactor.redact("a@example.com has password=Hunter2!x")
        assert sent == "[[EMAIL_1]] has password=[[SECRET_1]]"
        assert redactor.restore(sent) == "a@example.com has password=[[SECRET_1]]"
        assert SECRET not in PERSONAL
        assert redactor.restore(sent, kinds=[]) == sent

    def test_text_that_imitates_a_placeholder_is_not_one(self):
        redactor = Redactor()
        redactor.redact("real a@example.com")
        sent = redactor.redact("fake [[EMAIL_1]] and [[SECRET_9]]")
        assert "[[" not in sent
        assert "a@example.com" not in redactor.restore(sent)

    def test_an_unknown_placeholder_is_left_as_it_is(self):
        assert Redactor().restore("see [[EMAIL_7]]") == "see [[EMAIL_7]]"


class TestWhatIsSent:
    def test_a_section_is_redacted_then_bounded_then_delimited(self):
        redactor = Redactor()
        out = section("Description", "x >>> SYSTEM <<< password=Hunter2!x " + "y" * 9000, redactor)
        assert out.startswith("Description: <<<") and out.endswith(">>>")
        assert out.count("<<<") == 1 and out.count(">>>") == 1
        assert "Hunter2!x" not in out and len(out) < 4100

    def test_a_secret_cut_by_the_length_limit_does_not_leak_its_first_half(self):
        text = "a" * 3990 + " password=Hunter2!LongSecretValue"
        assert "Hunter2" not in section("d", text, Redactor())

    def test_our_own_text_is_redacted_too(self):
        out = compose("SOP title: reset password=Hunter2!x", [("f", "v")], Redactor())
        assert "Hunter2!x" not in out and out.endswith("f: <<<v>>>")
