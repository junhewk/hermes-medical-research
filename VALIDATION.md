# Release candidate validation: 0.4.0

Validation began on 2026-09-13 on headless Linux. **Release qualification is still pending.**
The behavioral candidate is `7ba0983` in the isolated test checkout (the same code was applied to
the main checkout as `2f92adc`). Production gateway settings and the original hypertension draft
were not changed.

- All 153 deterministic tests pass. They cover the earlier workflow contracts plus atomic batch
  submission, aggregate validation, pending/unavailable assessment, estimate and harms semantics,
  source-grounded overlap mappings, review freshness and resumable finalization.
- Ruff, bundle consistency, Codex plugin validation and the report skill validator pass.
- The pinned Hermes integration passes installation scanning with the unchanged Plugin Guard
  policy (`safe`, 32 medium and one low finding), disabled/enabled discovery, short-name index,
  qualified and bare-name loading, reference serving and slash invocation. This is a loader test,
  separate from model-driven report qualification.
- Wheel, source distribution and plugin ZIP build. Their contents were checked for local sessions,
  credentials, caches, test-run artifacts and unrelated workspace state; none were included.

The initial candidate (`0d69a7a`, applied as `bc124a9`) completed these behavioral runs:

| Host | Completion | Review result |
| --- | --- | --- |
| Codex 0.154.0 | 34 terminal calls; 17 extractions; all 11 artifacts | Key fixture distinctions preserved |
| Claude Code 2.1.268 / Opus 5 | 94 host turns; 18 extractions; all 11 artifacts | Self-review corrected two unsupported premises before export |
| Hermes 0.21.2 / qwen3.8-flash-next, repeat 1 | 53 model calls / 59 tool calls; all 11 artifacts | Failed semantic qualification: assumed outcome-pool membership entered certainty reasons |
| Same Hermes, repeat 2 | 51 model calls / 58 tool calls; all 11 artifacts | The same unsupported outcome-pool inference remained |
| Same Hermes, repeat 3 | 53 model calls / 56 tool calls; all 11 artifacts | Unsupported certainty reasoning and overstatement in prose remained |

All Hermes tests kept `max_turns=150`. Completion alone did not qualify the initial candidate for
release. These failures led to explicit source-grounded review-versus-outcome membership mappings
and stronger review of factual premises in certainty, alignment, overlap and weighting rationales.
Fresh Codex, Claude Code and three sequential Hermes runs of the corrected candidate are pending.

A separate bounded live-source Codex run completed in 61 terminal calls on the initial candidate.
It retrieved 54 raw / 50 deduplicated records from Europe PMC, OpenAlex and ClinicalTrials.gov,
screened all 50, selected six of 12 included records for detailed assessment, recorded 21 extractions,
and produced all 11 artifacts with six citation identities verified online. The run preserved its
20-record/source and eight-full-text ceilings (six attempts used). It qualified its conclusions:
four full texts were unavailable, six included records were deferred, one eligibility decision was
uncertain, and important harms and applicability gaps remained. This bounded run is workflow
evidence, not an exhaustive or independently adjudicated medical review.

Live connectivity checks also retrieved Europe PMC literature, an OA XML article, Crossref DOI
metadata and a ClinicalTrials.gov record. Europe PMC citation traversal returned HTTP 503 maintenance
after four attempts. No successful live citation traversal is claimed.

## Reproducing the behavioral corpus

Run `uv run python scripts/behavioral_fixture.py /tmp/mdr-eval/HOST-RUN` in a clean checkout.
It initializes `HOST-RUN/run` with eight explicitly synthetic records and stored source documents.
Use a fresh headless host session with the candidate skill and matching installed CLI. Ask it to
create the hypertension/exercise report, compare benefits and harms, explain disagreements and
export Markdown, HTML and evidence tables. State that retrieval is frozen, online identity checks
are disabled (`finalize --offline`), all outputs must be labeled simulated, and writes belong only
in the test directory. The host must perform its own screening, appraisal, synthesis and review.
Do not correct its evidence files manually or count a resumed/repaired run as a fresh pass.

Inspect the actual exports as well as completion status. Check group means versus treatment effects,
within-arm changes, active comparators, credible intervals, missing harms versus zero events,
comparator-arm event attribution, overlapping reviews, unknown outcome-pool membership, unsupported
GRADE premises, modality rankings, and the absence of equivalence or safety claims from null/missing
data. Synthetic acceptance testing cannot establish medical accuracy or replace human adjudication.

# Hermes short-name integration correction

Checked on 2026-09-12 against Hermes 0.21.2 source revision
`044a77b3b6af4ce16138d42762f812a20b9f7a89`, using the Python 3.11 environment that runs the user's
gateway. Plugin 0.3.1 was installed and enabled; both qualified skills loaded. The missing bare name
was a startup-index/invocation integration gap, not evidence of a failed plugin registration.

Hermes's portable plugin adapter omits plugin skills from the startup index and bare-name lookup.
Adding the installed `skills/` directory to the profile's supported `skills.external_dirs` setting
made `medical-deep-research` appear in the index and list, load by bare name, serve references, and
expand through `/medical-deep-research`. A same-process check verified the configuration transition
and `/reload-skills` behavior. No gateway restart was performed. The installed source also passed
the unchanged Plugin Guard policy with verdict `safe`.

The README now includes this setup step. `scripts/validate_hermes.py` additionally tests these
user-facing surfaces on the CI-pinned Hermes revision. Earlier checks below tested qualified plugin
registration; they did not establish short-name or startup-index integration. The two are now
reported separately. These checks do not submit a model request or generate the clinical report.

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
