"""Validation and derivation helpers for financial identifiers."""

from __future__ import annotations

import re

BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
BIC_IN_TEXT_RE = re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")
IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")
LEI_RE = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
IMO_RE = re.compile(r"\bIMO[\s:#-]*(\d{7})\b", re.IGNORECASE)

# Subset of official IBAN lengths; unknown countries fall back to the generic 15-34 check.
IBAN_LENGTHS: dict[str, int] = {
    "AD": 24,
    "AE": 23,
    "AL": 28,
    "AT": 20,
    "AZ": 28,
    "BA": 20,
    "BE": 16,
    "BG": 22,
    "BH": 22,
    "BR": 29,
    "BY": 28,
    "CH": 21,
    "CR": 22,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "DO": 28,
    "EE": 20,
    "EG": 29,
    "ES": 24,
    "FI": 18,
    "FO": 18,
    "FR": 27,
    "GB": 22,
    "GE": 22,
    "GI": 23,
    "GL": 18,
    "GR": 27,
    "GT": 28,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IL": 23,
    "IQ": 23,
    "IR": 26,
    "IS": 26,
    "IT": 27,
    "JO": 30,
    "KW": 30,
    "KZ": 20,
    "LB": 28,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MC": 27,
    "MD": 24,
    "ME": 22,
    "MK": 19,
    "MR": 27,
    "MT": 31,
    "MU": 30,
    "NL": 18,
    "NO": 15,
    "PK": 24,
    "PL": 28,
    "PS": 29,
    "PT": 25,
    "QA": 29,
    "RO": 24,
    "RS": 22,
    "RU": 33,
    "SA": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
    "SY": 31,
    "TN": 24,
    "TR": 26,
    "UA": 29,
    "VG": 24,
    "XK": 20,
}


def normalize_bic(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def is_valid_bic(value: str) -> bool:
    return bool(BIC_RE.match(normalize_bic(value)))


def bic_country(value: str) -> str | None:
    bic = normalize_bic(value)
    return bic[4:6] if is_valid_bic(bic) else None


def bic8(value: str) -> str:
    """Head-office (8-char) form of a BIC; 'XXX' branch is equivalent to the head office."""
    return normalize_bic(value)[:8]


def normalize_iban(value: str) -> str:
    return re.sub(r"[\s-]+", "", value).upper()


def looks_like_iban(value: str) -> bool:
    return bool(IBAN_RE.match(normalize_iban(value)))


def is_valid_iban(value: str) -> bool:
    iban = normalize_iban(value)
    if not IBAN_RE.match(iban):
        return False
    expected = IBAN_LENGTHS.get(iban[:2])
    if expected is not None and len(iban) != expected:
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def iban_country(value: str) -> str | None:
    iban = normalize_iban(value)
    return iban[:2] if looks_like_iban(iban) else None


def is_valid_imo(number: str) -> bool:
    """IMO ship numbers: 7 digits, last is check digit of weighted sum (weights 7..2)."""
    if not re.fullmatch(r"\d{7}", number):
        return False
    total = sum(int(d) * w for d, w in zip(number[:6], range(7, 1, -1), strict=True))
    return total % 10 == int(number[6])
