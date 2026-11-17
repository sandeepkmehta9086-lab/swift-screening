"""Unit tests: identifiers, normalisation, token/name similarity, index candidate retrieval."""

from __future__ import annotations

import pytest

from swift_screening.cpm.identifiers import (
    bic8,
    bic_country,
    iban_country,
    is_valid_bic,
    is_valid_iban,
    is_valid_imo,
)
from swift_screening.lists.model import ListSnapshot, ListType, WatchlistEntry, content_hash
from swift_screening.matching.index import WatchlistIndex
from swift_screening.matching.normalize import canon_token, normalize_name, skeleton
from swift_screening.matching.similarity import name_similarity


# ------------------------------------------------------------------ identifiers
@pytest.mark.parametrize(
    "bic,ok",
    [
        ("DEUTDEFF", True),
        ("DEUTDEFFXXX", True),
        ("deutdeff500", True),
        ("DEUT DEFF", True),
        ("DEUTDEF", False),
        ("1EUTDEFF", False),
        ("DEUTDEFFXX", False),
    ],
)
def test_bic_validation(bic: str, ok: bool) -> None:
    assert is_valid_bic(bic) is ok


def test_bic_helpers() -> None:
    assert bic_country("SNCTRUMMXXX") == "RU"
    assert bic8("SNCTRUMM123") == "SNCTRUMM"
    assert bic_country("bad") is None


@pytest.mark.parametrize(
    "iban,ok",
    [
        ("GB82WEST12345698765432", True),
        ("GB82 WEST 1234 5698 7654 32", True),
        ("DE89370400440532013000", True),
        ("GB00WEST12345698765432", False),
        ("GB82WEST1234569876543", False),
        ("XX", False),
    ],
)
def test_iban_validation(iban: str, ok: bool) -> None:
    assert is_valid_iban(iban) is ok


def test_iban_country() -> None:
    assert iban_country("IR062960000000100324200001") == "IR"


@pytest.mark.parametrize("imo,ok", [("9123453", True), ("9123454", False), ("123", False)])
def test_imo_check_digit(imo: str, ok: bool) -> None:
    assert is_valid_imo(imo) is ok


# ------------------------------------------------------------------ normalisation
def test_normalize_case_punct_diacritics() -> None:
    n = normalize_name("  MÜLLER-Brandt,  Johann  ")
    assert [t.text for t in n.tokens] == ["muller", "brandt", "johann"]


def test_normalize_drops_honorifics_and_marks_particles() -> None:
    n = normalize_name("Sheikh Dr. Mohammed bin Khalid Al-Rashid")
    assert [t.text for t in n.tokens] == ["mohammed", "bin", "khalid", "al", "rashid"]
    assert [t.text for t in n.core] == ["mohammed", "khalid", "rashid"]


def test_normalize_legal_forms() -> None:
    n = normalize_name("Zarkhov Industrial Holdings Limited Liability Company")
    assert n.core_text == "zarkhov industrial" and n.legal_form_stripped
    assert normalize_name("OOO Rosnavtek").core_text == "rosnavtek"


def test_normalize_homoglyphs_and_zero_width() -> None:
    mixed = "Kоrnilоv"  # Cyrillic 'о'
    assert normalize_name(mixed).core_text == "kornilov"
    assert normalize_name("Kor​ni‍lov").core_text == "kornilov"


def test_normalize_scripts() -> None:
    assert normalize_name("Владимир Корнилов").core_text == "vladimir kornilov"
    arabic = normalize_name("محمد الرشيد")
    assert all(t.abjad for t in arabic.tokens)
    assert [t.text for t in arabic.tokens] == ["mhmd", "rshyd"]  # article removed
    assert normalize_name("李强明").core_text == "li qiang ming"


def test_abd_prefix_split() -> None:
    assert normalize_name("Abdulrahman").core_text == "abd rahman"
    assert normalize_name("Abd al-Rahman").core_text == "abd rahman"
    assert normalize_name("Abdel Rahman").tokens[0].canon == "abd"
    assert normalize_name("Abdullah").core_text == "abdullah"


@pytest.mark.parametrize(
    "a,b",
    [
        ("mohammed", "muhammad"),
        ("yevgeniy", "evgenii"),
        ("hussein", "husain"),
        ("youssef", "yusuf"),
        ("alexander", "aleksandr"),
        ("philip", "filip"),
    ],
)
def test_canonical_variants_meet(a: str, b: str) -> None:
    assert canon_token(a) == canon_token(b)


def test_skeleton() -> None:
    assert skeleton("rashid") == skeleton("rshyd") == "rshd"
    assert skeleton("mohammed") == "mhmd"


def test_normalize_is_idempotent() -> None:
    for name in ["Mohammed Khalid Al-Rashid", "OOO Rosnavtek", "李强明", "Dr. Ilse Fabelhaft"]:
        once = normalize_name(name).full_text
        assert normalize_name(once).full_text == once


# ------------------------------------------------------------------ similarity
def sim(a: str, b: str, **kw: bool) -> float:
    return name_similarity(normalize_name(a), normalize_name(b), **kw).score


ENTRY = "Mohammed Khalid Al-Rashid"


@pytest.mark.parametrize(
    "query,minimum",
    [
        ("Mohammed Khalid Al-Rashid", 1.0),
        ("MOHAMMED KHALID AL RASHID", 1.0),
        ("Al-Rashid Mohammed Khalid", 0.97),  # reordered
        ("Muhammad Khalid Al-Rashid", 0.95),  # transliteration
        ("Mohammed Al-Rashid", 0.85),  # missing middle
        ("M. Al-Rashid", 0.82),  # initial
        ("Mohammed Khalid Al-Rashidd", 0.95),  # typo
        ("Mohamed Kalid Rasheed", 0.85),  # several variants at once
    ],
)
def test_similarity_positive(query: str, minimum: float) -> None:
    assert sim(query, ENTRY) >= minimum


@pytest.mark.parametrize(
    "query", ["Mohammed Ali", "Khalid Smith", "John Rashidov", "Ahmed Hassan", "Mohammed"]
)
def test_similarity_single_shared_token_is_not_a_match(query: str) -> None:
    assert sim(query, ENTRY) < 0.82


def test_similarity_symmetric_enough() -> None:
    for a, b in [("Kim Chol Song", "Song Kim Chol"), ("Rosnavtek", "OOO Rosnavtek")]:
        assert abs(sim(a, b) - sim(b, a)) < 0.1


def test_similarity_spacing_and_concatenation() -> None:
    assert sim("Ros navtek", "Rosnavtek") >= 0.97
    assert sim("Rosnavtekoil", "Rosnavtek") >= 0.85
    assert sim("A.B.C. Petrochemical Trading", "ABC Petrochemical Trading Co") >= 0.9
    assert sim("ZarkhovIndustrial", "Zarkhov Industrial Holdings LLC") >= 0.95


def test_similarity_truncated_name() -> None:
    full = "Golden Crescent General Trading FZE"
    truncated = "GOLDEN CRESCENT GENERAL TRADIN"
    assert sim(truncated, full, truncated=True) >= 0.9


def test_short_tokens_not_overmatched() -> None:
    assert sim("Kim Lee", "Kim Chol Song") < 0.82
    assert sim("Ali", "Al-Nour Exchange Company") < 0.82


# ------------------------------------------------------------------ index
def _index(*names: str) -> WatchlistIndex:
    entries = [WatchlistEntry(list_id="T", entry_id=str(i), name=n) for i, n in enumerate(names)]
    snap = ListSnapshot(
        list_id="T",
        list_type=ListType.SANCTIONS,
        source="t",
        content_hash=content_hash(entries),
        entries=entries,
    )
    return WatchlistIndex([snap])


@pytest.mark.parametrize(
    "query",
    [
        "Kornilov Vladimir",
        "Vladimir Kornilow",
        "Vladimir Kronilov",
        "Vladmir Kornilov",
        "V Kornilov",
        "Kornilov",
        "Vla dimir Kornilov",
        "VladimirKornilov",
    ],
)
def test_index_retrieves_candidate_for_variants(query: str) -> None:
    idx = _index("Vladimir Sergeyevich Kornilov", "Unrelated Person", "Another Name")
    names = [idx.names[n].raw for n in idx.name_candidates(normalize_name(query))]
    assert "Vladimir Sergeyevich Kornilov" in names


def test_index_identifier_lookup_bic_branch() -> None:
    entries = [
        WatchlistEntry(
            list_id="T",
            entry_id="1",
            name="Bank",
            identifiers=[{"type": "BIC", "value": "SNCTRUMMXXX"}],
        )
    ]  # type: ignore[list-item]
    snap = ListSnapshot(
        list_id="T",
        list_type=ListType.SANCTIONS,
        source="t",
        content_hash=content_hash(entries),
        entries=entries,
    )
    idx = WatchlistIndex([snap])
    assert idx.lookup_identifier("BIC", "SNCTRUMM") == [0]
    assert idx.lookup_identifier("BIC", "SNCTRUMM123") == [0]
    assert idx.lookup_identifier("BIC", "SNCTRUMN") == []


def test_normalisation_fixed_point_with_ampersand() -> None:
    # found by hypothesis: "B&B®" used to give "b b r", then "bbr" on a second pass
    once = normalize_name("B&B®").full_text
    assert normalize_name(once).full_text == once


def test_latin_letter_inside_cyrillic_word() -> None:
    # found validating against the live EU list: Latin "x" slipped into a Cyrillic name
    assert normalize_name("Росэксxмбанк").core_text == normalize_name("Росэксхмбанк").core_text
    assert normalize_name("Kоrnilоv").core_text == "kornilov"  # majority Latin still maps to Latin
