# Validation status

## Deterministic qualification

The v0.5 cutover is covered by the repository test suite. Its new architecture cases verify:

- creation and resolution of opaque Run and Task IDs inside an isolated artifact root;
- bounded multi-record Selector routing, bounded source access, exact target coverage, and
  CLI-recorded state;
- idempotent replay from a fresh Selector session;
- rejection of extra records, wrong roles, changed packets/results, and stale submissions;
- independent Auditor identity, exhaustive finding/report targets, quote/locator validation, and
  immutable audit receipts;
- copy-on-import v0.4 migration and provenance archiving;
- dry-run Hermes bootstrap, clean profile contents, least toolsets, automatic Bot Mode discovery,
  unmanaged-profile refusal, and locally edited-file refusal;
- human-named Review/Cycle creation, stable zero-idle-cost probes, session-bound claim tokens,
  bounded 5/30-minute retry and third-attempt blocking, and notification Outbox state;
- dry-run cron fleet planning with six base jobs and a real-cadence job for each living Review;
- atomic search reservation/task linkage, crash-safe reservation repair, immutable materialized
  plans, the report-mode biomedical-index and zero-hit gates, and explicit optional-source outages;
- anonymous and API-key PubMed access, explicit any/all concept semantics across query compilation
  and ranking, and regression coverage for alternative intervention families;
- an eight-turn Searcher profile, one-command search execution, immediate bounded Selector
  continuation, absolute Routine entrypoints, targeted profile bootstrap, managed Routine editing
  through Hermes's public CLI, and auditable active-Cycle cancellation;
- frozen living-review strategy validation, cumulative canonical corpus merge, source-digest reuse
  receipts, changed-source invalidation, and predecessor-linked no-change checkpoints;
- the retained retrieval, evidence, appraisal, synthesis, verification, and export behavior.

Run locally with:

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run pytest
uv build
```

The final local run on 2026-09-16 collected and passed 182 tests; Ruff, `git diff --check`, wheel
construction, source-distribution construction, and a wheel-only `mdr` smoke test also passed.

## Hermes operational gates

Hermes itself is not available in every development environment. Operational qualification therefore
has two explicit gates and preserves its JSON output:

1. In an isolated Hermes home, bootstrap the six profiles, apply the managed cron fleet, and run two
   fresh Selector Routine executions against one synthetic article each. Each worker must claim its
   Task, inspect only its bounded source, submit one decision, and leave an accepted receipt.
2. Only if both Selector workers pass, run three clean end-to-end Reviews through the cron queue.
   Every Cycle must complete through Searcher, Selector, Extractor, Synthesizer, and Auditor.
3. Qualify one living Review with a changed refresh and one exact no-change refresh, verify coalescing,
   pause/resume, Outbox acknowledgement, retry/backoff, and gateway-restart recovery.

The harness is `scripts/qualify_hermes.py`. A failure of the first gate is a stop condition: do not
weaken the CLI boundary or fall back to private delegated children.

### Current Hermes result (2026-09-15)

On `jkworkstation`, Hermes Agent 0.21.2 accepted the public creation of all six isolated profiles.
The earlier two-session Selector terminal gate passed: both fresh chats returned the assigned `run_id` and
`task_id` with state `accepted`, and the harness independently confirmed one accepted receipt in each
Run. The retained artifact is
`/tmp/hermes-medical-research-v0.5-codex-20260915/selector-qualification-2.json` on that host.

On 2026-09-16, the six profiles were also installed into the live `/home/jk/.hermes` registry used
by Hermes Agent 0.21.3 and the macOS remote gateway. Bot Mode discovered them automatically, and a
manual Selector task submitted through the macOS Bot roster produced one accepted CLI receipt. This
result qualifies terminal access but predates ADR 0002. The cron fleet, multiplexed gateway, three
full Reviews, and living-review gates have not yet been qualified.

## Release and repository rename gate

The GitHub repository must remain at its current name until all three full Hermes runs pass. After a
passing qualification artifact is reviewed, rename the repository to `hermes-medical-research`,
update its description/topics, tag `v0.5.0`, and verify the GitHub-based `uv tool` and `pipx`
installation examples. There is no PyPI or plugin-ZIP release in this design.

After the gate, the intended rename is:

```bash
gh repo rename hermes-medical-research --repo junhewk/medical-deep-research-plugin
git remote set-url origin git@github.com:junhewk/hermes-medical-research.git
```
