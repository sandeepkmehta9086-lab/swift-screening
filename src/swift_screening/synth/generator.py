"""Synthetic SWIFT MT / ISO 20022 message generator (no real payment data, ever).

Produces structurally valid MT103, MT202, MT202COV, pacs.008 and pacs.009 (incl. COV) with
seeded random fictitious parties, and lets tests inject a target name into any screenable field.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

SYLLABLES = [
    "ka",
    "lo",
    "mer",
    "tin",
    "sa",
    "vor",
    "den",
    "pi",
    "ru",
    "bel",
    "fa",
    "gor",
    "hal",
    "ni",
    "qua",
    "zen",
    "ter",
    "wil",
    "bro",
    "che",
    "dal",
    "mon",
    "ves",
    "lin",
]
STREETS = ["Harbour Road", "Station Street", "Market Square", "Mill Lane", "Park Avenue"]
TOWNS = [
    ("GB", "LONDON"),
    ("DE", "FRANKFURT"),
    ("FR", "PARIS"),
    ("NL", "AMSTERDAM"),
    ("CH", "ZURICH"),
    ("SG", "SINGAPORE"),
    ("US", "NEW YORK"),
    ("JP", "TOKYO"),
]
BICS = [
    "BANKGB2LXXX",
    "DEUTDEFFXXX",
    "BNPAFRPPXXX",
    "INGBNL2AXXX",
    "UBSWCHZH80A",
    "DBSSSGSGXXX",
    "CHASUS33XXX",
    "BOTKJPJTXXX",
]
IBANS = [
    "GB82WEST12345698765432",
    "DE89370400440532013000",
    "FR1420041010050500013M02606",
    "NL91ABNA0417164300",
]


def fake_name(rng: random.Random, words: int = 2) -> str:
    return " ".join(
        "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 3))).capitalize()
        for _ in range(words)
    )


def fake_company(rng: random.Random) -> str:
    return f"{fake_name(rng, 1)} {rng.choice(['Trading', 'Logistics', 'Textiles', 'Foods'])} Ltd"


@dataclass
class PartySpec:
    name: str
    address: list[str] = field(default_factory=list)
    country: str = "GB"
    town: str = "LONDON"
    account: str | None = None
    bic: str | None = None


def random_party(rng: random.Random, *, company: bool = False) -> PartySpec:
    country, town = rng.choice(TOWNS)
    return PartySpec(
        name=fake_company(rng) if company else fake_name(rng, rng.choice([2, 3])),
        address=[f"{rng.randint(1, 200)} {rng.choice(STREETS)}"],
        country=country,
        town=town,
        account=rng.choice(IBANS),
    )


# ------------------------------------------------------------------------------------- MT
def _mt_headers(mt: str, sender: str, receiver: str, cov: bool = False) -> str:
    s = sender[:8] + "A" + sender[8:11]
    r = receiver[:8] + "X" + receiver[8:11]
    b3 = "{3:{108:SYNTH" + ("}{119:COV" if cov else "") + "}{121:" + str(uuid.UUID(int=7)) + "}}"
    return f"{{1:F01{s}0000000000}}{{2:I{mt}{r}N}}{b3}"


def _party_50k(p: PartySpec) -> str:
    lines = [f"/{p.account}"] if p.account else []
    lines += [p.name, *p.address, f"{p.town} {p.country}"]
    return "\n".join(lines)


def _party_59f(p: PartySpec) -> str:
    lines = [f"/{p.account}"] if p.account else []
    lines += [f"1/{p.name}", *(f"2/{a}" for a in p.address), f"3/{p.country}/{p.town}"]
    return "\n".join(lines)


def mt103(
    *,
    ordering: PartySpec | None = None,
    beneficiary: PartySpec | None = None,
    ordering_option: str = "K",
    beneficiary_option: str = "",
    remittance: str | None = None,
    field72: str | None = None,
    extra_fields: list[tuple[str, str]] | None = None,
    sender: str = "BANKGB2LXXX",
    receiver: str = "DEUTDEFFXXX",
    seed: int = 0,
    raw_ordering: str | None = None,
    raw_beneficiary: str | None = None,
) -> str:
    rng = random.Random(seed)
    ordering = ordering or random_party(rng)
    beneficiary = beneficiary or random_party(rng)
    o = (
        raw_ordering
        if raw_ordering is not None
        else (_party_59f(ordering) if ordering_option == "F" else _party_50k(ordering))
    )
    if raw_beneficiary is not None:
        b = raw_beneficiary
    elif beneficiary_option == "F":
        b = _party_59f(beneficiary)
    else:
        b = _party_50k(beneficiary)
    fields = [
        ("20", f"REF{rng.randint(100000, 999999)}"),
        ("23B", "CRED"),
        ("32A", "261115EUR12500,00"),
        (f"50{ordering_option}", o),
        ("52A", sender),
        ("57A", receiver),
        (f"59{beneficiary_option}", b),
    ]
    if remittance:
        fields.append(("70", remittance))
    fields.append(("71A", "SHA"))
    if field72:
        fields.append(("72", field72))
    fields += extra_fields or []
    body = "\n".join(f":{t}:{v}" for t, v in fields)
    return _mt_headers("103", sender, receiver) + "{4:\n" + body + "\n-}{5:{CHK:123456789ABC}}"


def mt202(
    *,
    beneficiary_institution: str = "DEUTDEFFXXX",
    intermediary: str | None = None,
    sender: str = "BANKGB2LXXX",
    receiver: str = "CHASUS33XXX",
    field72: str | None = None,
) -> str:
    fields = [("20", "FI2FI001"), ("21", "RELREF001"), ("32A", "261115USD1000000,00")]
    if intermediary:
        fields.append(("56A", intermediary))
    fields.append(("58A", beneficiary_institution))
    if field72:
        fields.append(("72", field72))
    body = "\n".join(f":{t}:{v}" for t, v in fields)
    return _mt_headers("202", sender, receiver) + "{4:\n" + body + "\n-}"


def mt202cov(
    *,
    ordering: PartySpec,
    beneficiary: PartySpec,
    remittance: str | None = None,
    sender: str = "BANKGB2LXXX",
    receiver: str = "CHASUS33XXX",
) -> str:
    fields = [
        ("20", "COV0001"),
        ("21", "UNDERLYING01"),
        ("32A", "261115USD50000,00"),
        ("58A", "DEUTDEFFXXX"),
        ("50K", _party_50k(ordering)),
        ("52A", "BANKGB2LXXX"),
        ("57A", "DEUTDEFFXXX"),
        ("59F", _party_59f(beneficiary)),
    ]
    if remittance:
        fields.append(("70", remittance))
    body = "\n".join(f":{t}:{v}" for t, v in fields)
    return _mt_headers("202", sender, receiver, cov=True) + "{4:\n" + body + "\n-}"


# ------------------------------------------------------------------------------------- MX
def _x(v: str) -> str:
    return escape(v)


def _pty(tag: str, p: PartySpec, *, structured: bool = True, extra: str = "") -> str:
    if structured:
        addr = (
            f"<PstlAdr><StrtNm>{_x(p.address[0] if p.address else 'Main Street')}</StrtNm>"
            f"<TwnNm>{_x(p.town)}</TwnNm><Ctry>{p.country}</Ctry></PstlAdr>"
        )
    else:
        addr = (
            "<PstlAdr>"
            + "".join(f"<AdrLine>{_x(a)}</AdrLine>" for a in p.address)
            + f"<AdrLine>{_x(p.town)} {p.country}</AdrLine></PstlAdr>"
        )
    return f"<{tag}><Nm>{_x(p.name)}</Nm>{addr}{extra}</{tag}>"


def _agt(tag: str, bic: str) -> str:
    return f"<{tag}><FinInstnId><BICFI>{bic}</BICFI></FinInstnId></{tag}>"


def _acct(tag: str, iban: str | None) -> str:
    return f"<{tag}><Id><IBAN>{iban}</IBAN></Id></{tag}>" if iban else ""


def pacs008(
    *,
    debtor: PartySpec | None = None,
    creditor: PartySpec | None = None,
    ultimate_debtor: PartySpec | None = None,
    ultimate_creditor: PartySpec | None = None,
    remittance: str | None = None,
    intermediary: str | None = None,
    instr_for_next_agent: str | None = None,
    structured_address: bool = True,
    transactions: int = 1,
    seed: int = 0,
    version: str = "pacs.008.001.08",
    with_app_hdr: bool = False,
    drop: set[str] | None = None,
) -> str:
    rng = random.Random(seed)
    drop = drop or set()
    txs = []
    for i in range(transactions):
        d = debtor if (debtor and i == 0) else random_party(rng)
        c = creditor if (creditor and i == 0) else random_party(rng)
        parts = [
            f"<PmtId><InstrId>I{i}</InstrId><EndToEndId>E2E{i}</EndToEndId>"
            f"<UETR>{uuid.UUID(int=i + 1)}</UETR></PmtId>",
            ""
            if "IntrBkSttlmAmt" in drop
            else '<IntrBkSttlmAmt Ccy="EUR">1500.00</IntrBkSttlmAmt>',
            "<IntrBkSttlmDt>2026-11-15</IntrBkSttlmDt><ChrgBr>SHAR</ChrgBr>",
            _agt("InstgAgt", "BANKGB2LXXX"),
            _agt("InstdAgt", "DEUTDEFFXXX"),
            _agt("IntrmyAgt1", intermediary) if intermediary else "",
            _pty("UltmtDbtr", ultimate_debtor, structured=structured_address)
            if ultimate_debtor and i == 0
            else "",
            "" if "Dbtr" in drop else _pty("Dbtr", d, structured=structured_address),
            _acct("DbtrAcct", d.account),
            _agt("DbtrAgt", "BANKGB2LXXX"),
            _agt("CdtrAgt", "DEUTDEFFXXX"),
            "" if "Cdtr" in drop else _pty("Cdtr", c, structured=structured_address),
            _acct("CdtrAcct", c.account),
            _pty("UltmtCdtr", ultimate_creditor, structured=structured_address)
            if ultimate_creditor and i == 0
            else "",
            f"<InstrForNxtAgt><InstrInf>{_x(instr_for_next_agent)}</InstrInf></InstrForNxtAgt>"
            if instr_for_next_agent and i == 0
            else "",
            f"<RmtInf><Ustrd>{_x(remittance)}</Ustrd></RmtInf>" if remittance and i == 0 else "",
        ]
        txs.append("<CdtTrfTxInf>" + "".join(parts) + "</CdtTrfTxInf>")
    doc = (
        f'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:{version}"><FIToFICstmrCdtTrf>'
        f"<GrpHdr><MsgId>MSG{seed}</MsgId><CreDtTm>2026-11-15T10:00:00</CreDtTm>"
        f"<NbOfTxs>{transactions}</NbOfTxs><SttlmInf><SttlmMtd>INDA</SttlmMtd></SttlmInf></GrpHdr>"
        + "".join(txs)
        + "</FIToFICstmrCdtTrf></Document>"
    )
    if not with_app_hdr:
        return '<?xml version="1.0" encoding="UTF-8"?>' + doc
    hdr = (
        '<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">'
        "<Fr><FIId><FinInstnId><BICFI>BANKGB2LXXX</BICFI></FinInstnId></FIId></Fr>"
        "<To><FIId><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></FIId></To>"
        "<BizMsgIdr>B1</BizMsgIdr><MsgDefIdr>pacs.008.001.08</MsgDefIdr>"
        "<CreDt>2026-11-15T10:00:00Z</CreDt></AppHdr>"
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Envelope>{hdr}{doc}</Envelope>'


def pacs009(
    *,
    debtor_bic: str = "BANKGB2LXXX",
    creditor_bic: str = "DEUTDEFFXXX",
    cov_debtor: PartySpec | None = None,
    cov_creditor: PartySpec | None = None,
    intermediary: str | None = None,
) -> str:
    cov = ""
    if cov_debtor or cov_creditor:
        rng = random.Random(1)
        d = cov_debtor or random_party(rng)
        c = cov_creditor or random_party(rng)
        cov = (
            "<UndrlygCstmrCdtTrf>"
            + _pty("Dbtr", d)
            + _agt("DbtrAgt", "BANKGB2LXXX")
            + _agt("CdtrAgt", "DEUTDEFFXXX")
            + _pty("Cdtr", c)
            + "</UndrlygCstmrCdtTrf>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08"><FICdtTrf>'
        "<GrpHdr><MsgId>FI1</MsgId><CreDtTm>2026-11-15T10:00:00</CreDtTm><NbOfTxs>1</NbOfTxs>"
        "<SttlmInf><SttlmMtd>INDA</SttlmMtd></SttlmInf></GrpHdr><CdtTrfTxInf>"
        "<PmtId><InstrId>I1</InstrId><EndToEndId>E1</EndToEndId></PmtId>"
        '<IntrBkSttlmAmt Ccy="USD">1000000.00</IntrBkSttlmAmt>'
        + (_agt("IntrmyAgt1", intermediary) if intermediary else "")
        + _agt("Dbtr", debtor_bic)
        + _agt("Cdtr", creditor_bic)
        + cov
        + "</CdtTrfTxInf></FICdtTrf></Document>"
    )
