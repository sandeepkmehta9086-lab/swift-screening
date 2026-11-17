# Governance

Screening logic decides whether payments involving sanctioned parties are caught. Changes that
could lower sensitivity are therefore governed, not just reviewed.

## Governance group

A steering group, ideally including people with sanctions-compliance experience, owns:

- default thresholds (`config/tenants.example.yaml`) and the recall floor;
- matching and normalisation logic (`src/swift_screening/matching/`);
- policy and reason-code semantics (`src/swift_screening/decision/`);
- the frozen regression suite (`tests/regression/`) and fixture lists.

These paths are listed in `.github/CODEOWNERS` and need governance approval.

## Rules

1. The frozen regression suite only grows. Cases are never removed, weakened or skipped.
2. No change merges if recall on the regression suite or the generated benchmark drops.
3. Every PR states its sensitivity impact explicitly, even when it is "none".
4. A change that lowers sensitivity (higher thresholds, fewer variants, dropped fields) needs
   explicit approval from a compliance-role member, recorded in the PR.
5. Fail-closed behaviour (parse errors, stale/missing lists, timeouts, internal errors -> at
   least REVIEW) is not configurable away.
