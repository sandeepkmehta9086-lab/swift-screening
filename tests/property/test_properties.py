"""Property-based robustness tests (hypothesis)."""

from __future__ import annotations

import contextlib

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from swift_screening.config.models import TenantConfig
from swift_screening.decision.models import Decision
from swift_screening.matching.normalize import normalize_name
from swift_screening.matching.similarity import name_similarity
from swift_screening.parsing.errors import ParseError
from swift_screening.parsing.mt.parser import parse_mt
from swift_screening.parsing.mx.parser import parse_mx
from swift_screening.service import ScreeningService
from swift_screening.synth.generator import PartySpec, mt103

FAST = settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)

mt_alphabet = st.sampled_from(list("{}:-/\n\r 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabc,.'+?()"))
mt_like = st.lists(mt_alphabet, max_size=400).map("".join)
mt_fragments = st.lists(
    st.sampled_from(
        [
            "{1:F01BANKGB2LAXXX0000000000}",
            "{2:I103DEUTDEFFXXXXN}",
            "{3:{121:abc}}",
            "{4:\n",
            "\n-}",
            "{5:{CHK:1}}",
            ":20:REF\n",
            ":32A:261115EUR1,00\n",
            ":50K:/123\nNAME\n",
            ":59:BENE\n",
            ":59F:1/X\n",
            ":70:TEXT\n",
            ":72:/BNF/X\n//Y\n",
            ":71A:SHA\n",
            ":50F:\n",
            ":57A:\n",
            "}",
            "{",
            ":",
            "\n",
            "-",
        ]
    ),
    max_size=25,
).map("".join)


@FAST
@given(st.one_of(st.text(max_size=500), mt_like, mt_fragments))
def test_mt_parser_never_crashes(raw: str) -> None:
    try:
        cpm = parse_mt(raw)
    except ParseError:
        return
    # if it parsed, every party/free text has provenance
    for p in cpm.parties:
        assert p.provenance or p.role.value in {"sender", "receiver"}


xml_fragments = st.lists(
    st.sampled_from(
        [
            "<?xml version='1.0'?>",
            '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">',
            "<FIToFICstmrCdtTrf>",
            "<GrpHdr><MsgId>1</MsgId></GrpHdr>",
            "<CdtTrfTxInf>",
            "</CdtTrfTxInf>",
            "<Cdtr><Nm>X</Nm></Cdtr>",
            "<Dbtr>",
            "</Dbtr>",
            "<Nm>",
            "</Nm>",
            "&amp;",
            "&x;",
            "<!--c-->",
            "</FIToFICstmrCdtTrf>",
            "</Document>",
            "<",
            ">",
            "]]>",
            "<![CDATA[",
            "<RmtInf><Ustrd>",
        ]
    ),
    max_size=30,
).map("".join)


@FAST
@given(st.one_of(st.binary(max_size=500), xml_fragments.map(str.encode)))
def test_mx_parser_never_crashes(raw: bytes) -> None:
    with contextlib.suppress(ParseError):
        parse_mx(raw)


@FAST
@given(st.text(max_size=80))
def test_normalisation_idempotent_and_safe(name: str) -> None:
    n1 = normalize_name(name)
    n2 = normalize_name(n1.full_text)
    assert n2.full_text == n1.full_text
    assert all(t.text.isascii() for t in n1.tokens)


@FAST
@given(st.text(min_size=1, max_size=60), st.text(min_size=1, max_size=60))
def test_similarity_bounded(a: str, b: str) -> None:
    s = name_similarity(normalize_name(a), normalize_name(b)).score
    assert 0.0 <= s <= 1.0


@FAST
@given(st.text(alphabet=st.characters(categories=["L", "Zs"]), min_size=3, max_size=40))
def test_identical_names_always_match_themselves(name: str) -> None:
    n = normalize_name(name)
    if len(n.joined) >= 4 and len(n.core) >= 1 and all(len(t.text) >= 2 for t in n.core):
        assert name_similarity(n, n).score >= 0.97


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(st.one_of(st.text(max_size=300), mt_fragments))
def test_service_never_passes_unparseable_input(
    service: ScreeningService, tenant: TenantConfig, raw: str
) -> None:
    r = service.screen_mt(raw, tenant)
    if any(i.severity == "error" for i in r.parse_issues):
        assert r.decision != Decision.PASS
        assert "PARSE_FAILED" in r.reason_codes


@settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    st.text(alphabet=st.sampled_from(list("ABCDEFGHIJKLMNOPQRSTUVWXYZ .-")), max_size=40),
    st.sampled_from(["KIM CHOL SONG", "ROSNAVTEK", "NATALIA IGOREVNA VORONTSOVA"]),
)
def test_listed_name_in_noise_is_always_caught(
    service: ScreeningService, tenant: TenantConfig, noise: str, listed: str
) -> None:
    """A listed name embedded anywhere in remittance noise must always be caught."""
    text = f"{noise[:20]} {listed} {noise[20:]}"
    r = service.screen_mt(mt103(remittance=text[:140], beneficiary=PartySpec(name="A B")), tenant)
    assert r.decision != Decision.PASS
