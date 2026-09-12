# Release validation: 0.3.1

Checked on a headless Linux server on 2026-09-12.

- Python 3.12: all 131 deterministic tests, Ruff, and bundle consistency checks pass. Codex plugin
  and both Agent Skills validators pass. Wheel, source distribution, and plugin ZIP build.
- The local-file URL rejection test uses a harmless temporary fixture; rejection of local files,
  loopback URLs, and embedded URL credentials remains covered. Production URL validation is unchanged.
- Hermes 0.21.2, source commit `eec131b7163a8f287a9bddfc8ba11e6bd07ac49e`: the actual installation
  core clones the committed source tree locally and passes Plugin Guard with its default policy.
  The complete source, including tests, is scanned; no scanner rules are changed and no force flag
  is used. The verdict is `safe`; medium execution/supply-chain findings remain visible.
- The installed package exposes no skills while disabled. After enabling without tool-override
  privileges, a fresh process's `skills_list` returns both qualified skills. `skill_view` reads their
  instructions and every Markdown reference. All of this runs in a temporary Hermes profile.
- The report skill's normal qualified name is
  `agent-plugin-medical-deep-research-plugin-71b62b59:medical-deep-research`.
  Plugin Doctor's tool/hook counts do not count skills. The pinned host's process-level discovery
  cache is separate from installed/enabled state and the standalone `/reload-skills` command.
- CI repeats the integration against the pinned upstream source using
  `scripts/validate_hermes.py`. This tests installation mechanics and actual skill serving without
  a model, a gateway restart, or changes to a user's Hermes installation. It does not test remote
  catalog admission or prove that an existing gateway has refreshed its registry.

The 0.3.0 validation below did **not** cover installation scanning or enabled-state/fresh-process
skill access. A subsequent clean-source scan reproduced the user's 28 findings: 26 medium `uv_run`,
one medium `python_subprocess`, and one critical `system_passwd_access` match in a negative test.
The critical match caused the blocked verdict. The test's fixture change removes that false
positive while keeping local-file rejection tested. This corrects the earlier validation gap;
it is not a claim that heuristic scanning establishes the absence of all security issues.

# Previous release validation: 0.3.0

Checked on a headless Linux server on 2026-09-12.

- Python 3.12: 131 deterministic tests covering legacy search behavior, all new question frameworks,
  source pagination, raw retrieval budgets, research stages, stale evidence, quote/identity gates,
  full-text retries, PDF pages/XML tables, export escaping, and package contents.
- Ruff and offline bundle checks pass. Wheel, source distribution, and reproducible plugin ZIP build.
  All three archives were inspected for accidental session files, credentials, and research runs.
- Codex CLI 0.154.0 accepts the Git marketplace format and installs/enables both shared skills
  through the plugin bundle. The Codex plugin manifest validator passes.
- Claude Code 2.1.268 strict validation accepts the plugin and marketplace manifests.
- Hermes 0.21.2, source commit `eec131b7163a8f287a9bddfc8ba11e6bd07ac49e`: the actual portable
  loader discovers both skills with no diagnostics, and Plugin Doctor passes runtime discovery,
  parsing, import, and registration. Hermes was tested from its source checkout; no desktop was used.

Live checks retrieved Europe PMC literature, acquired an OA XML article (31 source segments),
resolved its DOI through Crossref, and retrieved a ClinicalTrials.gov record. Europe PMC's citation
traversal endpoint returned HTTP 503 with a maintenance message after retries. That live component
remains subject to provider availability; its parsing/provenance contract passes offline tests.
Search and export artifacts disclose failed, omitted, capped, or unverified sources.

The test report uses synthetic evidence. It validates workflow behavior and provenance controls,
not medical accuracy or equivalence to independent human screening, appraisal, or a formal review.
