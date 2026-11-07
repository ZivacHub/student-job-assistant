"""Phase 5 (P1): the application text: formal Croatian, masculine forms, only facts."""

import pytest

from app.apply.texts import (
    MAX_SUBJECT,
    SIGNATURE_PLACEHOLDER,
    DraftFacts,
    build_draft_text,
    has_placeholder,
    one_line,
)


def facts(**kw) -> DraftFacts:
    values = dict(title="Konobar/ica", employer="Caffe Bar Luna d.o.o.", category="ugostiteljstvo",
                  source_name="studentski-poslovi.hr", availability="petkom i subotom navečer",
                  signature="Ivan Primjer\n091 000 0000", has_car=False)
    values.update(kw)
    return DraftFacts(**values)


# ---------------------------------------------------------------- the Croatian draft
def test_the_draft_is_a_short_formal_croatian_email():
    draft = build_draft_text(facts())
    assert draft.language == "hr"
    assert draft.subject == "Prijava na oglas: Konobar/ica – Caffe Bar Luna d.o.o."
    assert draft.body == (
        "Poštovani,\n\n"
        "javljam se na Vaš oglas „Konobar/ica“, objavljen na studentski-poslovi.hr.\n\n"
        "Student sam. Slobodan sam petkom i subotom navečer. U privitku šaljem životopis.\n\n"
        "U nadi da ćete me pozvati na razgovor, srdačno Vas pozdravljam.\n\n"
        "Ivan Primjer\n091 000 0000")


def test_it_uses_masculine_forms_and_the_formal_vi():
    body = build_draft_text(facts()).body
    assert "Student sam" in body and "Slobodan sam" in body and "Vaš oglas" in body
    for feminine in ("Studentica", "studentica", "slobodna", "Slobodna"):
        assert feminine not in body


def test_it_never_mentions_student_servis():
    draft = build_draft_text(facts())
    assert "servis" not in (draft.subject + draft.body).lower()


def test_it_claims_nothing_about_experience_or_skills():
    body = build_draft_text(facts()).body.lower()
    for claim in ("iskustv", "radio sam", "vještin", "vjestin", "certifikat", "stručn", "godina"):
        assert claim not in body


def test_the_availability_is_his_own_words_made_a_sentence_once():
    assert "Slobodan sam vikendom." in build_draft_text(facts(availability="vikendom.")).body
    assert "Slobodan sam vikendom." in build_draft_text(facts(availability="  vikendom  ")).body
    assert "Slobodan" not in build_draft_text(facts(availability=None)).body
    assert "Slobodan" not in build_draft_text(facts(availability="   ")).body


def test_the_car_is_mentioned_only_where_it_matters_and_only_with_a_car():
    assert "Imam vlastiti automobil." in build_draft_text(
        facts(category="dostava", has_car=True)).body
    assert "automobil" not in build_draft_text(facts(category="dostava", has_car=False)).body
    assert "automobil" not in build_draft_text(facts(category="ugostiteljstvo", has_car=True)).body
    assert "automobil" not in build_draft_text(facts(category=None, has_car=True)).body


def test_the_cv_is_mentioned_only_when_one_is_attached():
    assert "U privitku šaljem životopis." in build_draft_text(facts()).body
    assert "privitku" not in build_draft_text(facts(cv_attached=False)).body


def test_a_pasted_ad_has_no_place_it_was_published_on():
    for manual in ("Pasted ads", "Ručno dodani oglasi", None):
        body = build_draft_text(facts(source_name=manual)).body
        assert "objavljen" not in body and body.count("javljam se na Vaš oglas „Konobar/ica“.") == 1


def test_without_an_employer_the_subject_is_just_the_job():
    assert build_draft_text(facts(employer=None)).subject == "Prijava na oglas: Konobar/ica"


# ---------------------------------------------------------------- text from an ad is untrusted
def test_a_title_with_line_breaks_and_control_characters_stays_on_one_line():
    draft = build_draft_text(facts(title="Konobar\r\nBcc: someone@example.com\x00\t vikend"))
    assert "\n" not in draft.subject and "\r" not in draft.subject and "\x00" not in draft.subject
    assert draft.subject.startswith("Prijava na oglas: Konobar Bcc: someone@example.com vikend")
    assert "\x00" not in draft.body


def test_a_very_long_title_is_cut_and_the_subject_stays_short():
    draft = build_draft_text(facts(title="Dostavljač " * 80, employer="Firma " * 40))
    assert len(draft.subject) <= MAX_SUBJECT and draft.subject.endswith("…")


def test_text_from_an_ad_can_never_look_like_a_place_to_fill_in():
    draft = build_draft_text(facts(title="Konobar [[ime]]", employer="Firma [[x]]"))
    assert not has_placeholder(draft.subject + draft.body)
    assert "[ime]" in draft.body


# ---------------------------------------------------------------- places still to fill in
def test_without_a_signature_the_draft_has_a_place_to_fill_in_and_cannot_pass_for_ready():
    draft = build_draft_text(facts(signature=None))
    assert draft.body.endswith(SIGNATURE_PLACEHOLDER) and has_placeholder(draft.body)
    assert has_placeholder(build_draft_text(facts(signature="   ")).body)
    assert not has_placeholder(build_draft_text(facts()).body)


@pytest.mark.parametrize("text, expected", [
    ("Poštovani,\n[[Ime i prezime]]", True),
    ("Poštovani [m/ž]", False),  # single brackets are ordinary text
    ("[[x]", False),
    ("", False),
    (None, False),
])
def test_only_double_brackets_count_as_a_place_to_fill_in(text, expected):
    assert has_placeholder(text) is expected


# ---------------------------------------------------------------- English CV, English draft
def test_an_english_cv_gets_an_english_draft_without_gendered_or_croatian_wording():
    draft = build_draft_text(facts(title="Junior developer", cv_language="en",
                                   category="it", employer="Tech d.o.o."))
    assert draft.language == "en"
    assert draft.subject == "Application: Junior developer – Tech d.o.o."
    assert draft.body.startswith("Dear Sir or Madam,")
    assert "I have attached my CV." in draft.body and "Kind regards,\nIvan Primjer" in draft.body
    assert "servis" not in draft.body.lower() and "Poštovani" not in draft.body


def test_one_line_helper():
    assert one_line("  a \n b\t c ") == "a b c"
    assert one_line(None) == ""
