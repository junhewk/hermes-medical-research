# Review preparation

Use `research init ... --mode review-prep --records-per-source N|all --fulltexts N|all`.
This prepares an auditable dossier for human reviewers. It does not implement independent
dual-reviewer screening or turn a single agent's work into a completed systematic review.

1. Specify the original question, eligibility, outcomes, databases, language/date restrictions,
   and exact limits. Treat the protocol as immutable; revisions receive a new workspace.
2. Create each child search with `plan question.json --mode review --limit-per-source N|all
   --research-run RUN --json`. Show exact per-source queries, selected variants, degradations,
   filters, and digest. Obtain approval for that displayed strategy.
3. Run `approve SEARCH --strategy-digest DIGEST`, then `preflight SEARCH`.
   A failed selected source stops review retrieval. Correct access or revise the protocol/strategy;
   do not silently drop a database from the approved search.
4. For `all`, show the returned counts and get explicit confirmation before calling
   `search SEARCH --confirm-all TOKEN`. For bounded searches use `search SEARCH`.
5. Resume interrupted retrieval with the same search command. Attach completed or failed runs
   using `research attach-search RUN SEARCH`; failures and truncation remain in the dossier.
6. Record every screening decision, study link, extraction, appraisal, and synthesis. Uncertain
   records, unavailable full texts, capped retrieval, and unassessed records stay visible.
7. Verify and export. Provide the protocol, exact search history, selection counts, exclusion
   log, evidence tables, provisional appraisals, report, and bibliography for human review.

Use the [PRESS guideline](https://pubmed.ncbi.nlm.nih.gov/27005575/) as a search-quality checklist:
question translation, Boolean/proximity logic, subject headings, text words, syntax, and limits.
An agent's check is not an independent librarian peer review. Report strategies, sources/platforms,
dates, restrictions, updates, and deduplication consistent with
[PRISMA search reporting](https://www.prisma-statement.org/prisma-search).

Report terminology follows the actual work. Title/abstract exclusions differ from full-text
exclusions. Records differ from reports of one underlying study. A partial API result count is
not an exhaustive database search, and a registry record is not a published outcome result.
Licensed-database imports and direct CENTRAL/Embase/Web of Science connectors are outside v0.3.0.
