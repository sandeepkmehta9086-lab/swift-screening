# ADR-0001: Phase 1 architecture and the canonical payment model

Status: accepted (2026-11-15)

## Context

Phase 1 needs deterministic, auditable screening of MT103/202/202COV and pacs.008/009, in
Python, with no LLM dependency and 100% recall on a frozen known-hit suite.

## Decisions

1. **Canonical Payment Model** (`cpm/models.py`). Both parsers map into one Pydantic model;
   every party and text keeps provenance (field path, line). Raw name lines are never discarded.
2. **Parse errors degrade, they do not abort.** Structural problems become
   `ParseWarning(severity="error")`. Whatever was extracted is still screened, and the decision
   is forced to at least REVIEW (`PARSE_FAILED`). If nothing can be parsed, every raw line is
   still screened.
3. **Redundant extraction.** A name line is screened as a name, joined with the next line, and as
   free text, so names split across lines or hidden in addresses are found.
4. **Recall-safe candidate retrieval.** An in-memory index keys every name token by canonical
   spelling, one-edit deletions (raw and canonical), consonant skeleton (vowel-less scripts),
   whole joined forms and prefixes. Retrieval runs on distinctive tokens; generic words
   ("trading", "ltd") only contribute by exact membership. A cheap plausibility check mirrors
   the scorer's rules before full scoring.
5. **Multi-view similarity.** The best of token alignment, joined compare, containment (single
   token side only) and prefix (truncation). A match needs two distinct source words (one
   distinctive), so a shared surname or "General Trading" alone does not hit.
6. **Python policy engine** with fail-closed rules that cannot be configured away. OPA/Rego comes
   in Phase 3 behind the same interface.
7. **Persistence.** Results and a hash-chained audit log in one transaction (SQLAlchemy; Postgres
   in production, SQLite for tests). The audit chain contains no personal data. Stateless mode
   stores redacted results only.
8. **List snapshots on the filesystem**, versioned by publication time + content hash, with
   diffs, freshness measured from the last successful source check, and a shrink guard.

## Deviations from Agent.md

- The synthetic generator lives in `src/swift_screening/synth/` (importable by tests) instead
  of `tools/synth/`.
- UK sanctions list connector not shipped: its source format must be verified first (section 9).
- ISO 20022 XSDs are not vendored (licence to be confirmed). A structural check is used unless
  XSDs are placed in `schemas/iso20022/`.
- Rate limiting is in-process. Several replicas need a shared store (Phase 3/4).
- Batch API and case-management UI are Phase 2 items and are not in this phase.

## Validation against the live OFAC / UN / EU lists (2026-11-15)

Fictional fixtures could not reveal problems that only real list data shows. Running the
connectors and engine against the official files (OFAC SDN 19,565 entries, UN 1,007, EU 6,269;
counts match each file's own record count) found and led to fixing:

| Finding | Fix |
|---|---|
| ~26k OFAC `idList` items are attributes (Gender, Vessel Year of Build, Website, ...) and were indexed as identifiers: an ID value "Male" would exact-match thousands of entries and BLOCK | Attributes kept separately; index ignores implausible identifiers |
| Every common name alerted, most BLOCKed ("John Smith" -> alias "John", "Maria Garcia" -> vessel "MARIA", "1 Market Square" -> "JSC Square", "SINGAPORE SG" -> "SG-DEVELOPMENT") | One-word list names match inside longer text only when distinctive; vessels only in vessel context or by IMO/MMSI; OFAC weak aliases whole-field only; a 2-3 letter token is never sole evidence; under half of a long name is no match |
| Exact two-word common names auto-BLOCKed | Name-only BLOCK needs >= 3 distinctive words (`thresholds.block_min_name_tokens`); shorter names REVIEW |
| Latin letter inside a Cyrillic word was garbled | Homoglyph mapping follows the word's majority script |
| A truncated EU download | Correctly rejected; last good snapshot kept (F.4) |

Result on the live lists: 100% recall on 758 attack variants of 25 sampled real entries; 16 of
35 common names REVIEW (genuine matches to real aliases such as "ALI, Hassan" or "Sergej
Petrov"), none BLOCK; p95 latency 24 ms. Resolving genuine common-name matches needs secondary
data (DOB, nationality) and is the Phase 2 precision scorer's job.

Accepted trade-offs (reported, not hidden):

- short one-word list names and weak aliases are not matched inside free text;
- first name + final surname of a long name always reaches REVIEW ("Ivan Meza" for "Ivan
  Atzayacatl Castaneda Meza"). This catches realistic short forms ("Muhammad Al-Rawi") at the
  cost of some extra reviews ("Juan Perez" vs "Juan Manual Medina Perez"); never BLOCK.

## Offline real-name regression and full-stack test (Phase 0 completion)

- `fixtures/lists/public_ofac_sample.json`: 200 OFAC SDN entries (public domain) so real name
  shapes are tested in every CI run. Benchmark on it: 100% recall over 10,815 variants, 0/200
  random and 4/589 hard-negative false positives (the 4 are hits on one-word aliases that are
  the whole name). Found and fixed: first+last short forms, honorific-prefixed concatenations
  (suffix keys), Mexican legal forms, mixed-script lookalikes, 3-letter vessel names.
- `tests/corpus/`: 250 valid + 250 invalid messages; every invalid one is never PASSed.
- `docker compose` (API + PostgreSQL) end-to-end: 16 e2e tests, PostgreSQL audit chain under
  60 concurrent requests, tamper detection, persistence across restarts. The restart test found
  that an unchanged list without a publication date got a new version on every reload
  (breaking idempotency); unchanged content now keeps its version.

## Consequences

The recall benchmark (every list name x 17 obfuscations x 8 fields) is 100% with 0 false
positives on 300 random and 74 hard-negative names; p95 latency is well under 150 ms at 50k
entries. Real lists have different name distributions, so the benchmark must be rerun when
real lists are loaded, and thresholds tuned by the governance group.
