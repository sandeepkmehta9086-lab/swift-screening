# Contributing

Read [`Agent.md`](Agent.md) first: it defines the prime directives, phases and scenario IDs.

1. Pick a task and the scenario IDs it covers (Agent.md section 5).
2. Write tests first and tag them: `@pytest.mark.scenario("C.7")`. Add frozen regression cases
   for scenarios marked **[R]**.
3. Implement behind the existing interfaces.
4. Run `make lint type test perf` and `make scenarios`; compare benchmark output with `main`.
5. In the PR, state the scenarios covered, the benchmark delta and the sensitivity impact.

Rules:

- No real payment data, ever. Use `swift_screening.synth` to generate messages.
- Test watchlist names must be fictional or taken from public lists with a source note.
- Never log names, IBANs or message bodies.
- Conventional Commits (`feat(matching): ...`, `fix(parsing): ...`).
