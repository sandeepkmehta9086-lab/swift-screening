"""Canonical Payment Model (CPM).

Both MT and MX parsers map into these models; everything downstream works only on the CPM.
Changing these models requires an ADR (see docs/adr/0001-canonical-payment-model.md).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

CPM_VERSION = "1.0"

SourceFormat = Literal["MT", "MX"]


class Provenance(BaseModel):
    """Points back to the exact place in the source message a value came from."""

    source_format: SourceFormat
    message_type: str
    path: str  # e.g. "MT:59F/2" or "MX:CdtTrfTxInf[0]/Cdtr/Nm"
    line: int | None = None  # 0-based line within the field
    start: int | None = None  # char offset within the line/field text
    end: int | None = None


class PartyRole(StrEnum):
    ORDERING_CUSTOMER = "ordering_customer"
    ULTIMATE_DEBTOR = "ultimate_debtor"
    BENEFICIARY = "beneficiary"
    ULTIMATE_CREDITOR = "ultimate_creditor"
    ORDERING_INSTITUTION = "ordering_institution"
    SENDERS_CORRESPONDENT = "senders_correspondent"
    RECEIVERS_CORRESPONDENT = "receivers_correspondent"
    THIRD_REIMBURSEMENT = "third_reimbursement"
    INTERMEDIARY = "intermediary"
    ACCOUNT_WITH_INSTITUTION = "account_with_institution"
    BENEFICIARY_INSTITUTION = "beneficiary_institution"
    DEBTOR_AGENT = "debtor_agent"
    CREDITOR_AGENT = "creditor_agent"
    INSTRUCTING_AGENT = "instructing_agent"
    INSTRUCTED_AGENT = "instructed_agent"
    PREVIOUS_INSTRUCTING_AGENT = "previous_instructing_agent"
    SENDER = "sender"
    RECEIVER = "receiver"
    MENTIONED = "mentioned"


class IdentifierType(StrEnum):
    BIC = "BIC"
    LEI = "LEI"
    IBAN = "IBAN"
    ACCOUNT = "ACCOUNT"
    NATIONAL_ID = "NATIONAL_ID"
    PASSPORT = "PASSPORT"
    CUSTOMER_ID = "CUSTOMER_ID"
    CLEARING_CODE = "CLEARING_CODE"
    IMO = "IMO"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    PLACE_OF_BIRTH = "PLACE_OF_BIRTH"
    OTHER = "OTHER"


class Identifier(BaseModel):
    type: IdentifierType
    value: str
    country: str | None = None
    valid: bool | None = None  # e.g. IBAN checksum result; None = not checked
    provenance: Provenance | None = None


class PostalAddress(BaseModel):
    lines: list[str] = Field(default_factory=list)
    street: str | None = None
    building: str | None = None
    post_code: str | None = None
    town: str | None = None
    country_sub_division: str | None = None
    country: str | None = None  # ISO 3166-1 alpha-2


class Party(BaseModel):
    role: PartyRole
    name: str | None = None
    name_lines: list[str] = Field(default_factory=list)  # raw name/address lines, never discarded
    address: PostalAddress | None = None
    country: str | None = None
    country_of_residence: str | None = None
    identifiers: list[Identifier] = Field(default_factory=list)
    party_type: Literal["individual", "organisation", "institution", "unknown"] = "unknown"
    sequence: str | None = None  # e.g. "B" for MT202COV underlying customer sequence
    transaction_index: int | None = None
    provenance: list[Provenance] = Field(default_factory=list)


class FreeText(BaseModel):
    kind: str  # "remittance", "sender_to_receiver", "regulatory", "unmapped", "instruction"
    text: str
    code: str | None = None  # e.g. "INS", "BNF" for field 72 codewords
    transaction_index: int | None = None
    provenance: Provenance


class ParseWarning(BaseModel):
    """A problem found while parsing. severity="error" forces at least REVIEW (fail closed)."""

    code: str
    message: str
    path: str | None = None
    severity: Literal["warning", "error"] = "warning"


class CanonicalPayment(BaseModel):
    cpm_version: str = CPM_VERSION
    message_id: str | None = None
    uetr: str | None = None
    source_format: SourceFormat
    message_type: str
    amount: Decimal | None = None
    currency: str | None = None
    value_date: date | None = None
    parties: list[Party] = Field(default_factory=list)
    remittance_info: list[FreeText] = Field(default_factory=list)
    instructions: list[FreeText] = Field(default_factory=list)
    regulatory_reporting: list[FreeText] = Field(default_factory=list)
    unmapped_fields: list[FreeText] = Field(default_factory=list)
    purpose_codes: list[str] = Field(default_factory=list)
    related_reference: str | None = None
    possible_duplicate: bool = False
    transaction_count: int = 1
    raw_hash: str
    parse_warnings: list[ParseWarning] = Field(default_factory=list)

    @property
    def has_parse_errors(self) -> bool:
        return any(w.severity == "error" for w in self.parse_warnings)

    def all_free_text(self) -> list[FreeText]:
        return [
            *self.remittance_info,
            *self.instructions,
            *self.regulatory_reporting,
            *self.unmapped_fields,
        ]
