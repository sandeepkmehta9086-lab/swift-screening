"""Name normalisation shared by list indexing and query screening.

Pipeline (both sides go through the same steps so they meet in the middle):

1. Unicode NFKC, strip zero-width / soft-hyphen / control characters.
2. Per word: mixed Latin + Cyrillic/Greek words are treated as homoglyph attacks and mapped
   visually (Cyrillic "а" -> "a"); pure foreign-script words are transliterated (anyascii).
   Arabic-script words lose the definite article and are flagged ``abjad`` (vowels are not
   written, so they are compared on consonant skeletons too). CJK is split per character.
3. ASCII lower-case, punctuation -> space, apostrophes removed.
4. Tokens are classified: honorifics and stop-words dropped, Arabic particles and legal
   forms kept only in the "full" form, initials marked.
5. Each token gets a ``canon`` (transliteration-robust spelling) and ``skel`` (consonants).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import cached_property, lru_cache

from anyascii import anyascii

ZERO_WIDTH_RE = re.compile("[­͏؜ᅟᅠ឴឵᠎​-‏‪-‮⁠-⁯﻿︀-️]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

CONFUSABLES = str.maketrans(
    {
        # Cyrillic
        "а": "a",
        "в": "b",
        "е": "e",
        "ё": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "і": "i",
        "ї": "i",
        "ј": "j",
        "ѕ": "s",
        "һ": "h",
        "ԁ": "d",
        "ӏ": "l",
        "ԛ": "q",
        "ԝ": "w",
        "ь": "b",
        "А": "A",
        "В": "B",
        "Е": "E",
        "Ё": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "У": "Y",
        "Х": "X",
        "І": "I",
        "Ј": "J",
        "Ѕ": "S",
        "Һ": "H",
        "Ԁ": "D",
        # Greek
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Υ": "Y",
        "Χ": "X",
        "α": "a",
        "ο": "o",
        "ν": "v",
        "ι": "i",
        "κ": "k",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "ε": "e",
    }
)

# Latin letters that look like Cyrillic ones (for Cyrillic words with Latin letters slipped in).
LATIN_TO_CYRILLIC = str.maketrans(
    {
        "a": "а",
        "e": "е",
        "o": "о",
        "p": "р",
        "c": "с",
        "y": "у",
        "x": "х",
        "k": "к",
        "m": "м",
        "h": "н",
        "t": "т",
        "b": "в",
        "i": "і",
        "A": "А",
        "E": "Е",
        "O": "О",
        "P": "Р",
        "C": "С",
        "Y": "У",
        "X": "Х",
        "K": "К",
        "M": "М",
        "H": "Н",
        "T": "Т",
        "B": "В",
    }
)

HONORIFICS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "miss",
        "mx",
        "dr",
        "prof",
        "professor",
        "sir",
        "dame",
        "lord",
        "lady",
        "madam",
        "mme",
        "mlle",
        "herr",
        "frau",
        "sheikh",
        "shaikh",
        "sheik",
        "shaykh",
        "haji",
        "hajji",
        "hadji",
        "hajj",
        "mullah",
        "maulana",
        "mawlana",
        "ustad",
        "ustadh",
        "engr",
        "hon",
        "rev",
        "jr",
        "sr",
        "ii",
        "iii",
    }
)
# Words that are ordinary in company names: matched, but weighted low so that two generic words
# ("... General Trading") cannot make a match on their own.
GENERIC_TERMS = frozenset(
    {
        "trading",
        "trade",
        "general",
        "international",
        "intl",
        "services",
        "service",
        "enterprise",
        "enterprises",
        "import",
        "export",
        "imports",
        "exports",
        "industries",
        "industrial",
        "shipping",
        "logistics",
        "energy",
        "commercial",
        "commerce",
        "investment",
        "investments",
        "finance",
        "financial",
        "capital",
        "global",
        "national",
        "united",
        "development",
        "construction",
        "engineering",
        "technology",
        "technologies",
        "solutions",
        "systems",
        "resources",
        "partners",
        "exchange",
        "foundation",
        "association",
        "trust",
        "management",
        "consulting",
        "oil",
        "gas",
        "petroleum",
        "marine",
        "maritime",
        "bank",
        "banking",
        "motors",
        "transport",
        "supply",
        "supplies",
        "industry",
        "products",
        "manufacturing",
        "agency",
        "network",
        "media",
        "medical",
        "pharma",
        "chemicals",
        "metals",
        "mining",
        "steel",
    }
)
STOPWORDS = frozenset({"the", "of", "and", "for", "a", "an"})
ARABIC_PARTICLES = frozenset({"al", "el", "ul", "bin", "ibn", "ben", "bint", "binti", "bte"})
LEGAL_FORM_TOKENS = frozenset(
    {
        "llc",
        "ltd",
        "limited",
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "llp",
        "lp",
        "gmbh",
        "ag",
        "kg",
        "sa",
        "sas",
        "sarl",
        "sl",
        "srl",
        "spa",
        "bv",
        "nv",
        "oy",
        "ab",
        "as",
        "aps",
        "kft",
        "zrt",
        "sro",
        "doo",
        "ooo",
        "zao",
        "oao",
        "pao",
        "ao",
        "jsc",
        "ojsc",
        "cjsc",
        "pjsc",
        "fze",
        "fzco",
        "fzc",
        "fzllc",
        "dmcc",
        "pte",
        "pvt",
        "pty",
        "bhd",
        "sdn",
        "tbk",
        "kk",
        "est",
        "establishment",
        "group",
        "holding",
        "holdings",
        "tov",
        "pp",
        "sp",
        "zoo",
        "sae",
        "wll",
        "lllp",
        "rao",
        "ou",
        "sapi",
        "cv",
    }
)
LEGAL_FORM_PHRASES = [
    "limited liability company",
    "public joint stock company",
    "closed joint stock company",
    "open joint stock company",
    "joint stock company",
    "private limited company",
    "public limited company",
    "limited liability partnership",
    "sp z o o",
    "s a",
    "s r l",
    "s p a",
    "b v",
    "n v",
    "l l c",
    "co ltd",
    "s de r l de c v",
    "s de rl de cv",
    "sa de cv",
    "s a de c v",
    "de c v",
]

# Known transliteration variant groups -> canonical spelling.
VARIANT_GROUPS: dict[str, tuple[str, ...]] = {
    "muhammad": (
        "mohammed",
        "mohammad",
        "mohamed",
        "mohamad",
        "muhammed",
        "muhamad",
        "mohd",
        "muhammet",
        "mehmet",
        "mohamud",
        "mohammod",
        "muhamed",
        "mahomed",
    ),
    "ahmad": ("ahmed", "ahmet", "ahmadh"),
    "yusuf": ("yousef", "youssef", "yousif", "yusef", "yosef", "yussuf", "yousuf", "jusuf"),
    "husayn": (
        "hussein",
        "husain",
        "hussain",
        "husein",
        "hussien",
        "hossein",
        "husseyn",
        "hussin",
        "hosein",
    ),
    "hasan": ("hassan", "hasen", "hassen"),
    "usama": ("osama", "usamah", "osamah", "oussama"),
    "abdullah": ("abdallah", "abdulla", "abdalla", "abdellah"),
    "umar": ("omar", "omer", "umer"),
    "uthman": ("othman", "osman", "usman", "osmane", "otman"),
    "khalid": ("khaled", "halid", "khalied"),
    "said": ("saeed", "sayed", "sayeed", "saeid", "saied", "sayid", "saïd"),
    "ibrahim": ("ebrahim", "ibraheem", "ibragim", "brahim"),
    "ismail": ("ismael", "esmail", "ismayil", "smail"),
    "mustafa": ("mostafa", "moustafa", "mustapha", "mostapha"),
    "mahmud": ("mahmoud", "mahmood", "mahmut"),
    "rashid": ("rasheed", "rachid", "rachied", "rashed"),
    "jamal": ("gamal", "djamal", "jamaal"),
    "abd": ("abdel", "abdul", "abdal", "abdol", "abdoul", "abdu", "abdel"),
    "yahya": ("yahia", "yehia", "yahiya"),
    "aleksandr": ("alexander", "aleksander", "alexandr", "oleksandr", "aleksandar", "alexandre"),
    "sergei": ("sergey", "serguei", "serhiy", "serhii", "sergiy"),
    "evgeni": ("yevgeny", "evgeny", "evgenii", "yevgeniy", "evgeniy", "yevhen", "eugene"),
    "dmitri": ("dmitry", "dmitriy", "dimitri", "dmytro", "dmitrii"),
    "andrei": ("andrey", "andriy", "andrii"),
    "aleksei": ("alexei", "alexey", "aleksey", "oleksiy", "oleksii", "alexis"),
    "yuri": ("yury", "yuriy", "iurii", "yurii", "jurij", "yurij"),
    "mikhail": ("mihail", "mykhailo", "michail"),
    "vladimir": ("volodymyr", "wladimir", "wladimyr"),
    "nikolai": ("nikolay", "mykola", "nicolai", "nikolaj"),
    "pyotr": ("petr", "pyotor", "petro"),
    "fyodor": ("fedor", "fiodor", "feodor"),
    "grigori": ("grigory", "grigoriy", "hryhoriy"),
}
VARIANT_LOOKUP: dict[str, str] = {
    v: canon for canon, variants in VARIANT_GROUPS.items() for v in (*variants, canon)
}
ABD_PREFIXES = ("abdel", "abdul", "abdal", "abdol", "abdoul")

_CANON_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p), r)
    for p, r in [
        (r"shch", "sh"),
        (r"sch", "sh"),
        (r"dzh", "j"),
        (r"dj", "j"),
        (r"zh", "j"),
        (r"tch", "ch"),
        (r"ph", "f"),
        (r"kh", "h"),
        (r"gh", "g"),
        (r"ck", "k"),
        (r"q", "k"),
        (r"c(?=[aouklrnmt]|$)", "k"),
        (r"x", "ks"),
        (r"w", "v"),
        (r"ou", "u"),
        (r"oo", "u"),
        (r"ee", "i"),
        (r"^ye", "e"),
        (r"^yu", "iu"),
        (r"^ya", "ia"),
        (r"(iy|ii|yi|ij|ey)$", "i"),
        (r"y$", "i"),
        (r"(?<=[^aeiou])y(?=[^aeiou])", "i"),
        (r"(.)\1+", r"\1"),
    ]
]
_VOWELS_RE = re.compile(r"[aeiouyw]")


def _script(ch: str) -> str:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "OTHER"
    if name.startswith("LATIN"):
        return "LATIN"
    if name.startswith("CYRILLIC"):
        return "CYRILLIC"
    if name.startswith("GREEK"):
        return "GREEK"
    if name.startswith("ARABIC"):
        return "ARABIC"
    if name.startswith("HEBREW"):
        return "HEBREW"
    if name.startswith(("CJK", "HIRAGANA", "KATAKANA", "HANGUL")):
        return "CJK"
    return "OTHER"


@lru_cache(maxsize=65536)
def canon_token(tok: str) -> str:
    if tok in VARIANT_LOOKUP:
        return VARIANT_LOOKUP[tok]
    out = tok
    for pat, rep in _CANON_RULES:
        out = pat.sub(rep, out)
    return VARIANT_LOOKUP.get(out, out)


@lru_cache(maxsize=65536)
def skeleton(tok: str) -> str:
    """Consonant skeleton used to compare vowel-less (Arabic/Hebrew) transliterations."""
    s = _VOWELS_RE.sub("", canon_token(tok))
    return re.sub(r"(.)\1+", r"\1", s)


@dataclass(frozen=True, slots=True)
class Token:
    text: str  # ascii lower
    canon: str
    skel: str
    abjad: bool = False  # came from Arabic/Hebrew script (no written vowels)
    initial: bool = False
    kind: str = "core"  # core | particle | legal | honorific | stop
    generic: bool = False
    word: int = 0  # index of the whitespace-delimited source word


@dataclass
class NormalizedName:
    """Immutable after construction; derived views are cached (instances shared via lru_cache)."""

    original: str
    tokens: list[Token] = field(default_factory=list)  # all tokens except honorific/stop
    legal_form_stripped: bool = False

    @cached_property
    def core(self) -> list[Token]:
        core = [t for t in self.tokens if t.kind == "core"]
        return core or [t for t in self.tokens if t.kind != "legal"] or list(self.tokens)

    @cached_property
    def core_text(self) -> str:
        return " ".join(t.text for t in self.core)

    @cached_property
    def full_text(self) -> str:
        return " ".join(t.text for t in self.tokens)

    @cached_property
    def joined(self) -> str:
        return "".join(t.text for t in self.core)

    @cached_property
    def joined_canon(self) -> str:
        return "".join(t.canon for t in self.core)

    @cached_property
    def full_joined(self) -> str:
        return "".join(t.text for t in self.tokens)

    @property
    def is_empty(self) -> bool:
        return not self.tokens


def clean_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = ZERO_WIDTH_RE.sub("", text)
    return CONTROL_RE.sub(" ", text)


def _transliterate_word(word: str) -> tuple[str, bool]:
    """Return (ascii text, abjad flag) for one whitespace-delimited word."""
    scripts = {_script(ch) for ch in word if ch.isalpha()}
    if not scripts:
        return anyascii(word), False
    if "LATIN" in scripts and scripts & {"CYRILLIC", "GREEK"}:
        # Homoglyph attack. Map towards the majority script of the word: a Latin name with
        # Cyrillic lookalikes ("Kоrnilоv") -> Latin; a Cyrillic name with Latin lookalikes
        # ("Росэксxмбанк") -> Cyrillic, then transliterate.
        foreign = [ch for ch in word if ch.isalpha() and _script(ch) in {"CYRILLIC", "GREEK"}]
        # If every Cyrillic/Greek letter has a Latin lookalike, the word is Latin in disguise.
        if all(ord(ch) in CONFUSABLES for ch in foreign):
            return anyascii(word.translate(CONFUSABLES)), False
        return anyascii(word.translate(LATIN_TO_CYRILLIC)), False
    if scripts & {"ARABIC", "HEBREW"}:
        w = word
        if w.startswith("ال") and len(w) > 3:
            w = w[2:]
        return anyascii(w), True
    if "CJK" in scripts:
        return " ".join(anyascii(ch) for ch in word), False
    return anyascii(word), False


Word = tuple[str, bool, int]  # (ascii text, abjad flag, source word index)


def to_ascii_words(text: str) -> list[Word]:
    """Clean + transliterate + lowercase + split.

    Each part remembers which whitespace-delimited source word it came from, so that a
    hyphenated surname ("Muller-Brandt") counts as one word when judging a match.
    """
    out: list[Word] = []
    word_idx = 0
    for word in clean_unicode(text).split():
        ascii_word, abjad = _transliterate_word(word)
        cjk = "CJK" in {_script(ch) for ch in word if ch.isalpha()}
        ascii_word = ascii_word.lower().replace("&", " and ")
        ascii_word = re.sub(r"['`´’‘]", "", ascii_word)
        for part in re.split(r"[^a-z0-9]+", ascii_word):
            if part:
                out.append((part, abjad, word_idx))
                if cjk:
                    word_idx += 1  # each CJK character is its own name element
        word_idx += 1
    return _collapse_letter_runs(out)


def _collapse_letter_runs(words: list[Word]) -> list[Word]:
    """Merge runs of >= 3 single letters ("A.B.C", "Q-i-a-n-g") back into one token."""
    out: list[Word] = []
    run: list[Word] = []

    def flush() -> None:
        if len(run) >= 3:
            out.append(("".join(w for w, _, _ in run), any(a for _, a, _ in run), run[0][2]))
        else:
            out.extend(run)
        run.clear()

    for item in words:
        if len(item[0]) == 1 and item[0].isalpha():
            run.append(item)
        else:
            flush()
            out.append(item)
    flush()
    return out


def _legal_phrase_positions(words: list[Word]) -> set[int]:
    """Indexes of words that belong to multi-word legal-form phrases."""
    texts = [w for w, _, _ in words]
    marked: set[int] = set()
    for phrase in LEGAL_FORM_PHRASES:
        p = phrase.split()
        for i in range(len(texts) - len(p) + 1):
            if texts[i : i + len(p)] == p:
                marked.update(range(i, i + len(p)))
    return marked


@lru_cache(maxsize=200_000)
def normalize_name(text: str) -> NormalizedName:
    # Split "abdulrahman" -> "abd" + "rahman" (but keep "abdullah").
    words: list[Word] = []
    for w, ab, wi in to_ascii_words(text):
        if VARIANT_LOOKUP.get(w) == "abdullah":
            words.append((w, ab, wi))
            continue
        for pre in ABD_PREFIXES:
            if w.startswith(pre) and len(w) > len(pre) + 2:
                words += [("abd", ab, wi), (w[len(pre) :], ab, wi + 1000)]
                break
        else:
            words.append((w, ab, wi))
    legal_idx = _legal_phrase_positions(words)
    # Drop honorifics / stop-words first, then collapse letter runs again, so that the output
    # is a fixed point ("B&B R" -> "b b r" would otherwise collapse to "bbr" on a second pass).
    kept = [
        (w, ab, wi, i in legal_idx)
        for i, (w, ab, wi) in enumerate(words)
        if w not in HONORIFICS and w not in STOPWORDS
    ]
    legal_flags = {(w, wi): is_legal for w, _, wi, is_legal in kept}
    words = _collapse_letter_runs([(w, ab, wi) for w, ab, wi, _ in kept])
    tokens: list[Token] = []
    stripped = False
    for w, ab, wi in words:
        if legal_flags.get((w, wi), False) or w in LEGAL_FORM_TOKENS:
            kind = "legal"
            stripped = True
        elif w in ARABIC_PARTICLES:
            kind = "particle"
        else:
            kind = "core"
        if w in {"abou", "abu"}:
            w = "abu"
        tokens.append(
            Token(
                text=w,
                canon=canon_token(w),
                skel=skeleton(w),
                abjad=ab,
                initial=len(w) == 1 and w.isalpha(),
                kind=kind,
                generic=w in GENERIC_TERMS,
                word=wi,
            )
        )
    return NormalizedName(original=text, tokens=tokens, legal_form_stripped=stripped)


def normalize_text_tokens(text: str) -> list[Token]:
    """Tokenise free text (no honorific/legal classification, positions preserved)."""
    out = []
    for w, ab, wi in to_ascii_words(text):
        kind = (
            "stop"
            if w in STOPWORDS
            else (
                "particle"
                if w in ARABIC_PARTICLES
                else (
                    "legal"
                    if w in LEGAL_FORM_TOKENS
                    else ("honorific" if w in HONORIFICS else "core")
                )
            )
        )
        out.append(
            Token(
                text=w,
                canon=canon_token(w),
                skel=skeleton(w),
                abjad=ab,
                initial=len(w) == 1 and w.isalpha(),
                kind=kind,
                generic=w in GENERIC_TERMS,
                word=wi,
            )
        )
    return out
