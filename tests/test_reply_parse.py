"""Plan 5.3b (D42): what a reply says, and whether Gmail vouches that it came from Ivan."""

import pytest

from app.apply.replyparse import (
    EMPTY,
    MIN_NEW_BODY,
    NO,
    SHORT,
    UNCLEAR,
    YES,
    above_the_quote,
    read_command,
    sender_check_passed,
    verdict_summary,
)

ENGLISH_QUOTE = """

On Tue, Nov 3, 2026 at 10:00 AM Ivan Primjer <ivan.test@gmail.com> wrote:

> NOVI OGLAS: Konobar
"""
CROATIAN_QUOTE = """

uto, 3. stu 2026. u 10:00 Ivan Primjer <ivan.test@gmail.com> napisao je:

> NOVI OGLAS: Konobar
"""
NEW_TEXT = "Poštovani,\nželim se javiti na vaš oglas za konobara.\nSrdačan pozdrav,\nIvan Primjer"


# ---------------------------------------------------------------- the first line decides
@pytest.mark.parametrize("first", ["DA", "da", "Da", "da.", "DA!", "  DA  ", "DA :)", "da,"])
def test_a_plain_da_is_a_yes_in_any_spelling(first):
    assert read_command(first + ENGLISH_QUOTE) == (read_command("DA"))
    assert read_command(first + ENGLISH_QUOTE).kind == YES
    assert read_command(first + ENGLISH_QUOTE).new_body is None


@pytest.mark.parametrize("first", ["NE", "ne", "Ne.", "NE!"])
def test_ne_is_a_no(first):
    assert read_command(first + ENGLISH_QUOTE).kind == NO


@pytest.mark.parametrize("quote", [ENGLISH_QUOTE, CROATIAN_QUOTE, "\n> citat\n"])
def test_the_quoted_original_never_counts(quote):
    command = read_command("DA" + quote)
    assert command.kind == YES and command.new_body is None


def test_a_wrapped_attribution_line_is_cut_too():
    text = ("DA\n\nOn Tue, Nov 3, 2026 at 10:00 AM Ivan Primjer\n"
            "<ivan.test@gmail.com> wrote:\n\n> NOVI OGLAS")
    assert above_the_quote(text) == ["DA"]


def test_text_under_da_replaces_the_body():
    command = read_command("DA\n" + NEW_TEXT + ENGLISH_QUOTE)
    assert command.kind == YES and command.new_body == NEW_TEXT


def test_the_new_text_ends_where_the_phone_signature_starts():
    command = read_command("DA\n" + NEW_TEXT + "\n\nSent from my iPhone" + ENGLISH_QUOTE)
    assert command.new_body == NEW_TEXT
    assert read_command("DA\n\nSent from my iPhone").new_body is None
    assert read_command("DA\n\nPoslano s mog Samsung uređaja").kind == YES


def test_text_under_ne_is_not_a_new_body():
    command = read_command("NE\nne šaljite, našao sam drugi posao" + ENGLISH_QUOTE)
    assert command.kind == NO and command.new_body is None


def test_a_few_words_under_da_are_refused_not_guessed():
    for text in ("DA\nhvala", "DA\nSrdačan pozdrav", "DA\n" + "x" * (MIN_NEW_BODY - 1)):
        assert read_command(text).kind == SHORT
    assert read_command("DA\n" + "x" * MIN_NEW_BODY).kind == YES


@pytest.mark.parametrize("text", ["Pošalji to", "DA, pošalji", "Danas ne", "yes", "OK", "daj"])
def test_anything_but_a_bare_da_or_ne_in_the_first_line_is_unclear(text):
    assert read_command(text + ENGLISH_QUOTE).kind == UNCLEAR


@pytest.mark.parametrize("text", ["", None, "\n\n", ENGLISH_QUOTE, "> samo citat"])
def test_a_reply_with_nothing_above_the_quote_is_empty(text):
    assert read_command(text).kind == EMPTY


def test_line_endings_of_any_kind_work():
    assert read_command("DA\r\n\r\nOn Tue, Nov 3 wrote:\r\n> x").kind == YES
    assert read_command("NE\r> x").kind == NO


# ---------------------------------------------------------------- Gmail's own verdict
GOOD = ("mx.google.com; dkim=pass header.i=@gmail.com header.s=20230601 header.b=AbCd; "
        "spf=pass (google.com: domain of ivan.test@gmail.com designates 1.2.3.4 as permitted "
        "sender) smtp.mailfrom=ivan.test@gmail.com; dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) "
        "header.from=gmail.com")
ME = "ivan.test@gmail.com"


def test_a_pass_from_gmails_own_server_is_accepted():
    assert sender_check_passed([GOOD], ME)


def test_a_folded_multiline_header_is_read_like_one_line():
    folded = GOOD.replace("; ", ";\r\n       ")
    assert sender_check_passed([folded], ME)


def test_dkim_alone_or_dmarc_alone_is_enough_when_it_names_the_from_domain():
    assert sender_check_passed(["mx.google.com; dkim=pass header.d=gmail.com"], ME)
    assert sender_check_passed(["mx.google.com; dmarc=pass header.from=gmail.com"], ME)


@pytest.mark.parametrize("header", [
    "mx.google.com; dkim=fail header.i=@gmail.com; spf=fail; dmarc=fail header.from=gmail.com",
    "mx.google.com; dkim=pass header.i=@evil.example; spf=fail; dmarc=fail header.from=gmail.com",
    "mx.google.com; dkim=pass header.i=@evil.example; dmarc=pass header.from=evil.example",
    "mx.google.com; spf=pass smtp.mailfrom=ivan.test@gmail.com",  # SPF alone is not enough
    "mx.google.com; dkim=neutral header.i=@gmail.com; dmarc=none header.from=gmail.com",
    "mx.evil.example; dkim=pass header.i=@gmail.com; dmarc=pass header.from=gmail.com",
    "dkim=pass header.i=@gmail.com; dmarc=pass header.from=gmail.com",  # no server name
    "mx.google.com",
])
def test_anything_less_than_a_pass_for_the_from_domain_is_refused(header):
    assert not sender_check_passed([header], ME)


def test_only_the_topmost_header_counts_a_fake_one_below_it_does_not():
    failing = "mx.google.com; dkim=fail header.i=@gmail.com; dmarc=fail header.from=gmail.com"
    assert not sender_check_passed([failing, GOOD], ME)
    assert sender_check_passed([GOOD, failing], ME)


def test_no_header_means_no():
    assert not sender_check_passed([], ME)
    assert not sender_check_passed([GOOD], "")
    assert not sender_check_passed([GOOD], "not-an-address")


def test_a_lookalike_domain_does_not_pass():
    header = "mx.google.com; dmarc=pass header.from=gmail.com.evil.example"
    assert not sender_check_passed([header], ME)
    assert not sender_check_passed([GOOD], "ivan.test@xgmail.com")


def test_blank_lines_before_the_answer_and_a_lone_attribution_tail_are_handled():
    assert read_command("\n\n  DA\n" + ENGLISH_QUOTE).kind == YES
    text = "DA\n\nIvan Primjer <ivan.test@gmail.com> wrote:\n> NOVI OGLAS"
    assert above_the_quote(text) == ["DA"]


def test_the_verdict_summary_holds_server_and_results_only():
    assert verdict_summary([GOOD]) == "mx.google.com: dkim=pass spf=pass dmarc=pass"
    assert verdict_summary([]) == "nema"
    assert verdict_summary(["mx.google.com"]) == "mx.google.com: bez rezultata"
    assert "header.b" not in verdict_summary([GOOD]) and "1.2.3.4" not in verdict_summary([GOOD])
