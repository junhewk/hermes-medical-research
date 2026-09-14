# Release candidate validation: 0.4.0

Status on 2026-09-15: **0.4.0 is published; full-report behavioral qualification did not pass.**
The release went out at the user's decision after the deterministic checks and the native-review
integration probes passed. The subsequent full-report runs below found false factual premises in
all three Hermes reports, each accepted by a passing native review. The Codex and Claude synthetic
reports had no false premise but discarded true information, and the live-source Codex report
passed an independent audit. Draft PR:
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

## Full-report behavioral qualification of 0.4.0 (2026-09-14 to 2026-09-15)

Released source `v0.4.0` (`92c7b30`), frozen at `/tmp/mdr-v040-r4` locally and
`jkworkstation:/tmp/mdr-v040-source-r4`. Synthetic runs use `scripts/behavioral_fixture.py`,
offline finalization and a shared 150-unit total. The live run used Europe PMC, OpenAlex and
ClinicalTrials.gov with 20 records per source and 8 full-text attempts. Hermes ran in isolated
profiles with qwen3.8-flash-next; production Hermes was not used. Each exported report was
fact-checked by an independent agent against the fixture text (or, for the live run, against
the stored source segments), including limitations, coverage reasons and appraisal rationales.

| Run | Completed | Native reviews (revise findings) | Accounting | Factual audit |
| --- | --- | --- | --- | --- |
| Codex synthetic | Yes, 15 min | 2: 3 revise, then all 7 pass | 64 author + 20 reviewer local tool calls of 150 | QUALIFIED: no false premise; over-hedging (below) |
| Codex live-source | Yes, 17 min | 1: all 5 pass | 79 author + 5 reviewer local tool calls of 150 | PASS: all estimates, interval types, limits and disclosures verified; two omissions |
| Hermes synthetic 1 | Yes, 164 min | 3: 2 revise, 3 revise, then all 6 pass | 80 author + 33 reviewer host iterations of 150 | FAIL (marginal): T4 called hypertensive and review-included; T3 SBP-pool contributor in coverage; unsupported ROBIS premise |
| Hermes synthetic 2 | Yes, 132 min | 2: 2 revise, then all 5 pass | 64 author + 24 reviewer host iterations of 150 | FAIL: T3 treated as R1 DBP and SBP pool contributor driving both certainty downgrades; SUCRA assigned to office SBP; syncope called serious |
| Hermes synthetic 3 | Yes, 112 min | 2: 3 revise, then all 7 pass | 52 author + 21 reviewer host iterations of 150 | FAIL: R1 misquoted as naming T1/T3 in its SBP pool, driving the SBP downgrade; "T2 reported no adverse events"; claimed source caveats that were dropped |
| Claude synthetic | Yes, 58 min (third attempt) | 4: 5 revise, 1 revise, 2 revise, then all 7 pass | 47 author + 54 reviewer local tool calls of 150 | QUALIFIED: no false premise; one unsupported appraisal premise (T2's unreported estimate called the prespecified analysis); T3 over-hedged |

Integration behavior held on every completed run: zero correction rounds were needed, every
review settled `completed`, Hermes reviewers ran the check tool 2-3 times per run, Hermes
reviewer denials were at most one per run, no author was blocked, Codex hooks denied nothing,
and every run stayed within its shared total. First reviews caught real errors that authors
then fixed (for example, "only T1 reported arm-level counts" and a misattributed ambulatory
gap). Two earlier Claude attempts stopped on the account's usage cap, not on the plugin; in the
second, the host ended the reviewer without a `SubagentStop` event and the receipt correctly
stayed `running` with its reservation held.

Reviews were not idle: across hosts, first reviews caught and authors fixed errors such as an
unhedged moderate-certainty pooled conclusion, false independence claims, and "only T1 reported
arm-level counts" while T4 reported a syncope. The core qualification criterion is still not met. The failure the native reviewer was introduced to
prevent, treating a review-level trial list as outcome-pool membership, recurred in Hermes runs
2 and 3 and was passed by the reviewer. Three plugin causes were identified; none is fixed in 0.4.0:

1. **Author-tagged outcome mappings are presented as proof.** The overlap validator accepts
   `scope: "outcome"` whenever the quote exists in the review and a finding outcome is named;
   it cannot tell whether the quote states per-outcome pool membership. The review packet then
   lists these mappings as `proven_outcome_mappings`, and the prompt tells reviewers to check
   certainty premises against them. In Hermes run 2 the author tagged "Two trials, T1 and T3,
   were included" as outcome scope for DBP, and the reviewer accepted T3 as a DBP contributor,
   although T3 reports no DBP. Run 3 tagged the same sentence differently for SBP and DBP and
   received opposite verdicts.
2. **Only findings are reviewed.** Limitations, coverage reasons, extraction labels and
   appraisal rationales are exported without review, and several false premises survived there
   after the findings were corrected.
3. **The citation contract encourages discarding true information.** The rule that record
   titles are not citable led the first Codex and Claude reviewers to demand removal of T3's
   handgrip and inactive-control labels, which the corpus supports, and both authors complied.
   Their exported coverage and study tables still carry the labels, so the files disagree.

Remaining release gates for the next revision: fix the three causes above, then repeat three
fresh Hermes reports, one Codex and one Claude synthetic report, and one bounded live-source
report, with the same independent factual audit covering limitations, coverage and appraisals.
Run artifacts: `/tmp/mdr-v040-eval/{codex,claude,live-codex}-r4*` locally and
`jkworkstation:/tmp/mdr-v040-eval/hermes-r4-{1,2,3}`.

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
