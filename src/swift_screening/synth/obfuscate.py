"""Name obfuscations used by the recall benchmark (one function per scenario family C.*)."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

HOMOGLYPHS = {
    "a": "а",
    "e": "е",
    "o": "о",
    "c": "с",
    "p": "р",
    "x": "х",
    "y": "у",
    "A": "А",
    "E": "Е",
    "O": "О",
    "C": "С",
    "P": "Р",
    "K": "К",
    "M": "М",
    "T": "Т",
}
DIACRITICS = {"a": "á", "e": "é", "i": "í", "o": "ö", "u": "ü", "n": "ñ", "c": "ç"}


def upper(n: str) -> str:
    return n.upper()


def lower_punct(n: str) -> str:
    return n.lower().replace(" ", " , ", 1) + "."


def strip_diacritics(n: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", n) if not unicodedata.combining(c))


def add_diacritics(n: str) -> str:
    out, done = [], 0
    for ch in n:
        if ch in DIACRITICS and done < 2:
            out.append(DIACRITICS[ch])
            done += 1
        else:
            out.append(ch)
    return "".join(out)


def _longest_token_index(tokens: list[str]) -> int:
    return max(range(len(tokens)), key=lambda i: len(tokens[i]))


def typo_substitute(n: str) -> str:
    t = n.split()
    i = _longest_token_index(t)
    w = t[i]
    pos = len(w) // 2
    t[i] = w[:pos] + ("x" if w[pos].lower() != "x" else "z") + w[pos + 1 :]
    return " ".join(t)


def typo_transpose(n: str) -> str:
    t = n.split()
    i = _longest_token_index(t)
    w = t[i]
    pos = len(w) // 2
    t[i] = w[: pos - 1] + w[pos] + w[pos - 1] + w[pos + 1 :]
    return " ".join(t)


def reorder(n: str) -> str:
    t = n.split()
    return " ".join(t[-1:] + t[:-1]) if len(t) > 1 else n


def drop_middle(n: str) -> str:
    t = n.split()
    return " ".join([t[0], t[-1]]) if len(t) >= 3 else n


def add_middle(n: str) -> str:
    t = n.split()
    return " ".join([t[0], "Ibrahim", *t[1:]]) if len(t) >= 2 else n


def initial_first(n: str) -> str:
    t = n.split()
    return " ".join([t[0][0] + ".", *t[1:]]) if len(t) >= 2 else n


def honorific(n: str) -> str:
    return f"Dr. {n}"


def separators(n: str) -> str:
    t = n.split()
    i = _longest_token_index(t)
    t[i] = "-".join(t[i])
    return " ".join(t)


def homoglyph(n: str) -> str:
    out, done = [], 0
    for ch in n:
        if ch in HOMOGLYPHS and done < 3:
            out.append(HOMOGLYPHS[ch])
            done += 1
        else:
            out.append(ch)
    return "".join(out)


def zero_width(n: str) -> str:
    return "​".join(n[i : i + 2] for i in range(0, len(n), 2))


def split_token(n: str) -> str:
    t = n.split()
    i = _longest_token_index(t)
    w = t[i]
    t[i] = w[:2] + " " + w[2:]
    return " ".join(t)


def concatenate(n: str) -> str:
    return n.replace(" ", "")


def phonetic(n: str) -> str:
    return (
        n.replace("ph", "f")
        .replace("Kh", "H")
        .replace("kh", "h")
        .replace("ck", "k")
        .replace("C", "K")
        .replace("ov", "off")
    )


OBFUSCATIONS: dict[str, Callable[[str], str]] = {
    "C.2-case": upper,
    "C.2-punct": lower_punct,
    "C.3-strip": strip_diacritics,
    "C.3-add": add_diacritics,
    "C.4-sub": typo_substitute,
    "C.4-transpose": typo_transpose,
    "C.5-reorder": reorder,
    "C.6-drop-middle": drop_middle,
    "C.6-add-middle": add_middle,
    "C.7-initial": initial_first,
    "C.8-honorific": honorific,
    "C.19-separators": separators,
    "C.20-homoglyph": homoglyph,
    "C.20-zero-width": zero_width,
    "C.21-split": split_token,
    "C.21-concat": concatenate,
    "C.22-phonetic": phonetic,
}
