Here’s a build plan for an open-source, AI-native SWIFT screening service. One principle runs through all of it: in sanctions screening, a missed hit costs far more than a false alert. So “self-healing” and “evolving” have to work inside guardrails, and the system should never quietly make itself less strict.

1. Scope and positioning

What it does: It takes SWIFT MT messages (MT103, MT202/202COV, MT101, MT199, etc.) and ISO 20022 MX messages (pacs.008, pacs.009, pacs.004, pain.001, camt.*). It pulls out every party, agent, location and free-text field, screens them against sanctions, PEP and watchlists, and returns a decision of Pass / Review / Block. Every decision comes with an explanation and a full audit trail.

What it is not: It’s not a replacement for a regulated institution’s compliance judgment. It’s a decision-support engine. Final dispositions on hits stay with humans, or with rules each deployer configures and is accountable for.

Licensing: Apache 2.0 for the code. Watchlist data comes with its own licences. OpenSanctions, for example, is free for non-commercial use but needs a commercial licence otherwise. Keep the data connectors pluggable and let each deployer bring their own data.

2. High-level architecture
                 ┌────────────────────────────────────────────┐
  Clients ──►    │  API Gateway (REST + gRPC, OpenAPI 3.1)     │  auth, rate limit, tenancy
                 └───────────────┬────────────────────────────┘
                                 ▼
                 ┌────────────────────────────────────────────┐
                 │ Ingestion & Normalization                   │
                 │  • MT parser (blocks 1-5, field tags)       │
                 │  • MX parser (XSD validation, CBPR+/HVPS+)  │
                 │  • → Canonical Payment Model (CPM)          │
                 └───────────────┬────────────────────────────┘
                                 ▼
                 ┌────────────────────────────────────────────┐
                 │ Entity Extraction (rules + LLM/NER)         │
                 │  names, addresses, BICs, IBANs, countries,  │
                 │  vessels, free-text (field 70/72, Ustrd)    │
                 └───────────────┬────────────────────────────┘
                                 ▼
                 ┌────────────────────────────────────────────┐
                 │ Screening Engine (ensemble)                 │
                 │  1. Exact/ID match (BIC, LEI, IMO)          │
                 │  2. Fuzzy name (Jaro-Winkler, phonetic,     │
                 │     transliteration, token-set)             │
                 │  3. Vector semantic match (embeddings)      │
                 │  4. Geo/country & embargo rules             │
                 │  5. ML risk scorer → calibrated score       │
                 └───────────────┬────────────────────────────┘
                                 ▼
                 ┌────────────────────────────────────────────┐
                 │ Decision & Explanation                      │
                 │  policy engine (OPA/Rego) + LLM rationale   │
                 └───────────────┬────────────────────────────┘
                                 ▼
             Case Mgmt UI  ◄──  Audit Log (immutable)  ──►  Feedback Loop
                                                            (evolution layer)
3. Core components
3.1 Message parsing (the foundation)
MT: Parse blocks {1}–{5}, validate field formats per message type (e.g. 50K/50F/59/59F, 52A/D, 57A/D, 70, 72), and handle the different party-option formats. Reference library: Prowide Core (Java, Apache 2.0), or write a Rust/Go parser for speed.
MX: Validate against the ISO 20022 XSDs and the CBPR+ usage guidelines. Extract the structured parties (Dbtr, Cdtr, UltmtDbtr, UltmtCdtr, InstgAgt, IntrmyAgt1-3), postal addresses and remittance info. Reference: Prowide ISO 20022.
MT↔MX translation: Useful for normalizing everything into one model, and for screening translated messages consistently (truncation during translation is a known blind spot).
Canonical Payment Model (CPM): One internal JSON schema that both formats map to. Everything downstream works only on the CPM.
3.2 Entity extraction
Deterministic extraction comes first, from structured fields.
An LLM or NER model handles unstructured content: field 70/72, Ustrd, 50K free-format lines, names embedded in addresses, vessel names, and hidden references (“on behalf of…”).
Output every entity with a provenance pointer back to the exact field and offset, which explainability depends on.
3.3 Watchlist management
Connectors for OFAC SDN and Consolidated lists, UN, EU, UK OFSI, other national lists, and OpenSanctions as an aggregator, plus private/internal lists.
Watch for list updates, then diff, version, and re-index. Every screening result records which list version it was checked against.
Pre-compute name variants: transliterations (Cyrillic, Arabic, Chinese, etc.), aliases, and phonetic keys.
3.4 Screening engine (hybrid, not LLM-only)
Recall layer: fast, deterministic, auditable (fuzzy + phonetic + token matching, plus exact IDs). This is the safety net and must never be removed.
Semantic layer: multilingual embeddings in a vector DB to catch spelling games, reordering and transliteration drift.
Precision layer: a gradient-boosted or small transformer model that scores candidate pairs using context (DOB, nationality, address, counterparty history) to rank and suppress false positives, never to hide true matches below the recall floor.
Contextual rules: embargoed jurisdictions, ports, BIC country codes, 50% ownership rules (if you have ownership data), and dual-use goods keywords.
3.5 Decision and explanation
A policy engine (e.g. Open Policy Agent) turns scores into Pass / Review / Block, with thresholds each tenant configures.
An LLM writes a plain-language rationale: “Creditor name ‘Ahmad Al-X’ matched SDN entry #12345 (alias) at 0.91; country and DOB consistent.” This is generated from the structured evidence, never instead of it.
3.6 Case management (minimal UI)
An alert queue, side-by-side message and list-entry view, four-eyes approval, and disposition reasons. These dispositions are also the training signal for the evolution layer.
4. The “AI-native, self-healing, evolving” layer
Capability	What it means here	Guardrail
Self-healing (ops)	Health probes, circuit breakers, automatic restarts, fallback from ML scorer to deterministic rules if models degrade, retries/DLQ for malformed messages	Fail closed (hold the message for review), never fail open
Self-healing (data)	Detect stale or failed list updates, auto-retry, alert when a list is older than its SLA	Block or flag screening if list freshness is broken
Self-healing (parsing)	An LLM proposes a parser fix when new or variant formats fail; the fix is tested against a golden corpus	Proposed fixes go through PR + CI, never hot-patched in production
Evolving (models)	Retrain the precision scorer on analyst dispositions; active learning surfaces uncertain cases	Shadow mode → champion/challenger → human sign-off before promotion
Evolving (rules)	An agent mines missed-hit and false-positive patterns and suggests new rules or threshold tweaks	Suggestions only; a change that lowers sensitivity needs explicit compliance approval
Drift monitoring	Track alert rates, score distributions and list-hit mix per corridor	Auto-roll back a model if recall on the regression suite drops

Non-negotiable: keep a frozen regression test suite of known true hits, including tricky transliterations, split names and hidden-in-free-text cases. No model, rule or parser change ships if recall on it drops. That’s what lets the system evolve safely, and it’s what regulators’ model-risk expectations (like SR 11-7-style governance) will look for.

5. Open API design

Endpoints (OpenAPI 3.1):

POST /v1/screen/mt takes a raw MT body (text/plain)
POST /v1/screen/mx takes ISO 20022 XML
POST /v1/screen/entity screens a single name/party
POST /v1/screen/batch handles async bulk work and returns a job ID; webhook on completion
GET /v1/results/{id} returns the full result with evidence and explanation
POST /v1/feedback/{id} takes an analyst disposition (feeds evolution)
GET /v1/lists/status returns list versions and freshness

Response contract (sketch):

json
{
  "screening_id": "scr_...",
  "decision": "REVIEW",
  "risk_score": 0.87,
  "list_versions": {"OFAC_SDN": "2026-11-13T18:00Z"},
  "hits": [{
    "field": "MX:CdtTrfTxInf/Cdtr/Nm",
    "input_value": "...",
    "matched_entry": {"list": "OFAC_SDN", "id": "...", "name": "..."},
    "match_type": ["fuzzy", "transliteration"],
    "score": 0.91,
    "explanation": "..."
  }],
  "model_version": "scorer-v12",
  "latency_ms": 42
}

Public access with safety:

API keys or OAuth2 client credentials, per-tenant rate limits, and a free public tier with tight quotas.
PII by default: don’t retain message bodies on the public instance (stateless mode). Store hashes and decisions only, unless a self-hosted tenant opts in.
Expect the public endpoint to be used for probing (“does this spelling evade the screen?”). Rate limiting, score bucketing on public responses (decision, not exact score), and abuse monitoring all help here.
SDKs for Python, Java, Go and TypeScript, generated from the OpenAPI spec.
6. Tech stack (suggested)
Core services: Go or Rust for parsing and screening (low latency); Python for ML and agents
Messaging: Kafka or NATS JetStream
Storage: Postgres (cases, config), OpenSearch (fuzzy/name search), pgvector or Qdrant (embeddings), S3-compatible object storage with WORM for audit
ML: PyTorch / XGBoost, MLflow for model registry, Evidently for drift
LLM: pluggable provider interface, so self-hosters can run open-weight models on-prem (many banks can’t send payment data to external APIs)
Policy: Open Policy Agent
Platform: Kubernetes + Helm, ArgoCD, OpenTelemetry, Prometheus/Grafana
Security: mTLS, Vault for secrets, field-level encryption, SBOM + signed images (Sigstore)
7. Phased roadmap

Phase 0 — Foundations (4–6 weeks)
Repo, governance, licence, contribution guide, canonical payment model, golden test corpus (synthetic MT/MX messages plus known-hit cases).

Phase 1 — MVP (2–3 months)
MT103/202 and pacs.008/009 parsing, OFAC + UN + EU lists, deterministic fuzzy screening, REST API, Pass/Review/Block, audit log, Docker Compose deployment.

Phase 2 — AI layer (2–3 months)
LLM/NER extraction for free text, embeddings-based matching, ML false-positive scorer in shadow mode, LLM explanations, basic case-management UI, feedback endpoint.

Phase 3 — Self-healing and evolution (3 months)
Ops self-healing, list-freshness guards, champion/challenger pipeline, drift monitoring, agent-suggested rules with an approval workflow, regression gates in CI.

Phase 4 — Ecosystem (ongoing)
More message types (pain.001, camt.056, MT101/199), ownership/UBO data, vessel/IMO screening, Helm charts, SDKs, plugin marketplace for list connectors, public hosted sandbox.

8. Things to settle early
Regulatory posture: publish clear docs saying deployers stay responsible for compliance. Consider a model card and validation pack so banks can run their own model risk reviews.
Test data: never use real payment data in the repo. Build a synthetic message generator plus a curated list of public sanctioned names for tests.
Benchmarks: publish recall/precision on an open benchmark set. That builds trust faster than features.
Governance: a steering group (ideally with people from compliance) to approve changes to default thresholds and matching logic.

Some details here come from my existing knowledge, not a fresh check: the current state of the MT→MX migration (CBPR+ coexistence was due to end in November 2025), the OpenSanctions licence terms, and the Prowide libraries’ current status. Check them before you commit to anything.
