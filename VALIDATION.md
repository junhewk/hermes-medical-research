# Release candidate validation: 0.4.0

Status on 2026-09-14: **released as 0.4.0 at the user's decision** after the deterministic
checks and the three native-review integration probes below passed. Full-report behavioral
qualification (fresh complete reports on each host and a live-source run) has not been
performed on this revision and remains open. Draft PR:
https://github.com/junhewk/medical-deep-research-plugin/pull/1
The original hypertension report remains unchanged.

## Why separate native review is required

The previous self-review candidate (`c7b42d9` in the test checkout, `e4b2f27` in the
main checkout) passed 155 deterministic tests and CI on Python 3.11/3.12, but failed
semantic qualification. All three final Qwen runs produced the 11 artifacts:

| Hermes / qwen3.8-flash-next | Model calls | Tool calls | Outcome |
| --- | ---: | ---: | --- |
| repeat 1 | 68 | 74 | Completed; not independently medically validated |
| repeat 2 | 68 | 71 | Failed: assumed DBP pool membership in GRADE; false absence of isometric trial |
| repeat 3 | 58 | 68 | Failed: unverified DBP pool membership in certainty rationale; overstated harms counts |

Each incorrect premise passed that author's self-review. File completion, quoted text and
structural consistency therefore cannot serve as semantic acceptance criteria.
Codex's revised fixture run completed in 38 terminal calls; Claude's final run in 84 host
turns. Both preserved the main fixture distinctions. A bounded live-source Codex run
produced 11 files, with six online identity checks and explicit unavailable evidence limits.
These results establish workflow behavior, not medical correctness. Europe PMC citation
traversal returned 503 maintenance; successful live traversal is not claimed.

## Native reviewer revision (in progress)

The replacement freezes every finding and the complete corpus, omits prior reviews/chat,
requires a distinct native task identity, and gates finalization on its recorded verdict.
A persistent shared budget charges author work, reviewers and retries; unknown termination
retains reservations. Hermes uses native iterations with code refunds disabled; Codex/Claude
conservatively count local tool calls through native lifecycle hooks. The latter is explicitly
not an API-request or hosted-web-tool cap. No new model API setup is introduced.

The first isolated native Claude reviewer found unsupported statements that self-review had
accepted. Its long JSON handoff was truncated, so the adapter correctly did not accept it.
The first isolated Hermes reviewer found several errors but missed the DBP certainty premise;
it also hit the host's 420-second synchronous tool timeout. These are failed integration/
quality probes, not release passes. The revision now gives each reviewer a dedicated result
file, highlights outcome-specific membership, and uses bounded asynchronous Hermes polling.
New frozen integration and semantic tests are required before release.

Regression tests cover distinct author/reviewer identities, whole-corpus freshness, missing
and invalid native output, quote checks, budget reservations, overruns, interrupted/resumed
accounting, per-session Hermes caps, native hook identities, duplicate events and restricted
reviewer file access. Current test counts and host results must be updated after the final
candidate is frozen. Do not promote this draft based on historical green CI alone.

## Native review stabilization (2026-09-14)

Diagnosis from the retained probe artifacts, before any code change:

- Codex r5 (`/tmp/mdr-native-eval/codex-review-r5`): delegation, packet delivery and the
  reviewer's verdict all worked. One of 129 citations used a bare record id and a JSON-path
  locator instead of a `document_id`/segment locator; the validator rejected the whole verdict,
  the receipt recorded `failed` without a reason, and the failure reached the author only as a
  Codex `systemMessage`, which Codex shows to the user rather than the model. The author
  therefore reported success on a failed receipt.
- Hermes r4 (`jkworkstation:/tmp/mdr-native-eval/hermes-review-r4`): finished as an accepted
  review (7 records, 4 revise, 3 pass; 8 reviewer + 12 author iterations of 60). The child
  had been denied `search_files` and `patch` by a name-based policy, the author polled at a
  45-second cap and tripped the host's identical-call guard, and acceptance depended on the
  loop's exit reason rather than the validated artifact. Hermes r2 had failed earlier because
  the child inherited no output limit and truncated a single ~20K-token write.

Changes: complete problem reporting with hints (`research review-check`, Hermes
`medical_research_review_check`), persisted `failure_reason`/`failure_detail`, a citation
contract and example in the frozen packet, per-finding result files, reviewer read-back and
result updates, at most two metered correction rounds on Codex/Claude through the host's
subagent-stop decision, model-facing review state in tool context, a bounded author stop
block, path-based Hermes reviewer policy with instructive messages, progress-bearing status
polls with waits up to 300 seconds, artifact-based Hermes acceptance, and a fail-closed
abandon path for reviewers the host never settles.

Deterministic evidence on this checkout: 188 tests (168 before this pass), Ruff, bundle validation, wheel/sdist/ZIP
builds, and `scripts/validate_hermes.py` against the pinned Hermes source (three tools, one
hook; Plugin Guard verdict `safe`, 40 medium and 1 low notices). Offline replay of
`review-check` against the retained r5 artifact reports exactly the one invalid citation with
the two valid document ids as a hint; against the Hermes r4 artifact it reports valid.

Integration probes of the frozen candidate (`/tmp/mdr-native-r6`, this working tree) on
copies of a synthetic failed report; nothing was repaired or finalized:

| Host | Outcome | Accounting (measured unit) |
| --- | --- | --- |
| Codex 0.154.0 | Receipt `completed`, 7 records all `revise`, 45 observations, 68 verified citations, 0 repairs. One denial: the reviewer's first relative-path read (now resolved against the host cwd). The author ran `research check` and reported the recorded state. | 8 author + 7 reviewer local tool calls of 60 |
| Claude Code | Receipt `completed`, 7 per-finding result files, reviewer ran `review-check` itself, 0 denials, 0 repairs. The author's final message distinguished the recorded verdict from the reviewer's chat summary. | 3 author + 12 reviewer local tool calls of 60 |
| Hermes 0.21.2 / qwen3.8-flash-next (isolated profile on jkworkstation) | Receipt `completed`, 7 per-finding result files (4 pass, 3 revise), reviewer ran `medical_research_review_check` and stopped with `exit_reason=completed`, 0 repairs. One denial: the host's `tool_describe` discovery helper (now allowed). Status polls at 300 s returned changing progress; no identical-call guard trips. | 8 author + 13 reviewer host iterations of 60 (r4: 12 + 8) |

These probes establish handoff, verdict validation, accounting and author visibility. They are
not full-report qualification: fresh complete Codex/Claude reports, three Hermes reports under
the shared 150 budget, and a bounded live-source run remain release gates.

## Reproducing behavioral qualification

Run `uv run python scripts/behavioral_fixture.py /tmp/mdr-eval/HOST-RUN` in a clean checkout.
Use the matching installed CLI and skill, native reviewer tools/hooks, a fresh author session,
and a shared 150-unit total. The eight-record corpus is explicitly synthetic. Freeze retrieval,
use `finalize --offline`, label outputs simulated, and write only in the test directory.
The author must perform screening, appraisal, synthesis, native review, corrections and export.
Do not manually correct evidence or count a repaired/resumed run as a fresh pass.

Inspect the actual reports for group means versus contrasts, within-arm changes, active
comparators, interval types, missing harms versus zero events, comparator event attribution,
review overlap, unknown outcome pools, GRADE premises and unsupported superiority/equivalence.
Record author plus all reviewer usage and retry costs. Three fresh Qwen report passes,
Codex/Claude native integration, package checks and a bounded live-source run remain release gates.

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
