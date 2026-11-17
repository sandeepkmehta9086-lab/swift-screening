# Security policy

Please report vulnerabilities privately to the maintainers rather than in public issues.
Include affected version, impact and reproduction steps. Do not include real payment data.

Areas of particular interest:

- ways to make a listed name evade screening (we treat these as security bugs: please send the
  pattern so it can be added to the frozen regression suite);
- XML parsing (XXE, entity expansion, resource exhaustion);
- tenant isolation of results, API key handling, rate limiting;
- personal data leaking into logs, audit rows or stored results in stateless mode.

Operational guidance: change the demo API key and database password, run behind TLS, keep the
public profile stateless, and store API key hashes only (`swift-screening hash-key`).
