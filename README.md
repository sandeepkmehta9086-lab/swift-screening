# swift-screening

Open-source sanctions screening for SWIFT MT and ISO 20022 (MX) payment messages, written in
Python. It parses a payment, pulls out every party, agent, identifier, location and free-text
field, screens them against sanctions / PEP / internal watchlists, and returns
**PASS / REVIEW / BLOCK** with per-hit evidence, an explanation and a full audit trail.

> **Decision support, not a compliance officer.** Deployers remain responsible for their
> sanctions obligations, list licences, thresholds and embargo configuration.

Status: **Phase 1 (deterministic MVP)** of [`swift_screening_roadmap.md`](swift_screening_roadmap.md).
How the project is built and extended is defined in [`Agent.md`](Agent.md).

## What Phase 1 does

| Area | Implemented |
|---|---|
| MT parsing | Blocks 1-5, MT103, MT202, MT202COV (sequence B), party options A/B/C/D/F/K/no-letter, 70/72 codewords, 77B, UETR, PDE/PDM, X-charset and line-limit checks |
| MX parsing | pacs.008, pacs.009 (incl. COV), AppHdr, all agents, ultimate parties, structured/hybrid/unstructured addresses, RmtInf, RgltryRptg; XXE / entity / depth / size protection; XSD validation when XSDs are supplied |
| Lists | OFAC SDN, UN Consolidated, EU FSF XML connectors; JSON/CSV internal lists; versioned snapshots, diffs, freshness SLA, shrink guard, retry with backoff |
| Matching | Exact IDs (BIC 8/11 incl. branches, LEI, IMO, passport, national ID); fuzzy names robust to typos, reordering, missing/extra names, initials, honorifics, Arabic particles, transliteration variants, Cyrillic/Arabic/CJK scripts, homoglyphs, zero-width characters, spacing/concatenation games, truncation and split lines; names hidden in free text and address lines |
| Geography | Embargoed countries from BIC / IBAN / address / 77B codes; sanctioned regions and countries named in free text (no 2-letter substring false positives) |
| Decisions | Tenant thresholds, PEP/internal/identifier actions, reason codes, fail-closed on parse errors, stale or missing lists, timeouts and internal errors |
| API | `POST /v1/screen/{mt,mx,entity}`, `GET /v1/results/{id}`, `GET /v1/lists/status`, `/healthz`, `/readyz`, `/metrics`; API keys, rate limits, size/content-type limits, idempotency, tenant isolation, public mode (decision only) |
| Audit | Hash-chained append-only audit log with `verify-chain`, stateless mode, retention purge, replay against recorded list versions |

## Quick start

```bash
uv sync
uv run swift-screening demo                  # loads fictional fixture lists, screens samples
uv run swift-screening serve                 # http://127.0.0.1:8080/docs

curl -s -X POST localhost:8080/v1/screen/mt \
  -H "X-API-Key: demo-key-change-me" -H "Content-Type: text/plain" \
  --data-binary @message.txt
```

With Docker (API + PostgreSQL): `docker compose up --build`
(set `SWIFT_SCREENING_PORT=18080` if host port 8080 is taken).
Full-stack test: `make e2e` (builds, starts, runs `tests/e2e`, verifies the audit chain, stops).

Load real lists (check each source's licence and current format first, see Agent.md section 9):

```bash
uv run swift-screening lists load --format ofac            # default OFAC URL
uv run swift-screening lists load --format un
uv run swift-screening lists load --format eu
uv run swift-screening lists load --format csv --list-id MYBANK_INTERNAL --list-type internal --source internal.csv
```

Then enable those list ids for your tenant in `config/tenants.example.yaml`.

## Testing

```bash
make test        # unit, integration, contract, property, regression, benchmark (coverage >= 90%)
make perf        # p95 latency gate with 50,000 list entries
make lint type   # ruff + mypy --strict
```

| Suite | What it proves |
|---|---|
| `tests/unit` | Parser rules (A.*, B.*), normalisation, similarity, index retrieval, geo, policy, list connectors |
| `tests/integration` | End-to-end scenarios (C.*, D.*, E.*), API behaviour (H.*), audit and data protection (I.*), CLI |
| `tests/regression` | **Frozen** known-hit suite (must stay 100%) + generated recall benchmark: every list name x 17 obfuscations x 8 fields, plus random and hard negatives |
| `tests/property` | Hypothesis fuzzing: parsers never crash, unparseable input never PASSes, listed names in noise are always caught |
| `tests/contract` | Checked-in OpenAPI matches the app; every Phase 1 scenario has a test |
| `tests/perf` | p95 < 150 ms per message at 50k entries |
| `tests/unit/test_golden_corpus.py` | 250 valid + 250 invalid on-disk messages (`tests/corpus`, `make corpus`) |
| `tests/regression/test_public_sample.py` | Recall / false positives on 200 real OFAC SDN entries (public domain), offline |
| `tests/e2e` | Running stack (docker compose): API, PostgreSQL audit chain, concurrency (opt-in) |

Scenario coverage is generated into [`docs/scenarios.md`](docs/scenarios.md).

Validated against the live OFAC / UN / EU files: `SWIFT_SCREENING_LIVE_LISTS=1 uv run pytest
tests/integration/test_live_lists.py` (opt-in; downloads ~60 MB). Findings and fixes are in
[`docs/adr/0001-phase1-architecture.md`](docs/adr/0001-phase1-architecture.md).

All test watchlists are **fictional** (`fixtures/lists`). The repository never contains real
payment data.

## Layout

```
src/swift_screening/
  cpm/         canonical payment model + identifier validation
  parsing/     mt/ and mx/ parsers
  extraction/  CPM -> screening subjects (names, text, identifiers, countries)
  lists/       connectors, snapshot store, refresh
  matching/    normalisation, similarity, candidate index, engine, geo rules
  decision/    policy engine, reason codes, explanations, result contract
  storage/     results + hash-chained audit log
  api/         FastAPI app
  synth/       synthetic message generator, obfuscations, benchmark
```

## Licence

Apache 2.0 for the code. Watchlist data carries its own licence terms.

## Run Steps
To stop it: docker compose down keeps the data, docker compose down -v also deletes the test database and
    list snapshots. To run it again: make e2e SWIFT_SCREENING_PORT=18080.
