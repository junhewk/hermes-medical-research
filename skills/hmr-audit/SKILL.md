---
name: hmr-audit
description: Independently check one frozen evidence group through hmr, citing an exact quote for every verdict.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical audit

You check one claimed audit task and never correct the author's work. A runner gives you an
instruction file with `run_id`, `task_id`, `packet_path`, `proposal_path`, and the exact `submit`
and `fail` commands. Read that file, then the packet. Use only the packet, the proposal, and the
supplied commands, replacing only their uppercase placeholders. Never run Python, heredocs,
scripts, or a code cell, and never edit a skill.

## The proposal is already written; you fill in the blanks

`proposal_path` holds a complete template. Every `target_id`, `field`, `assertion`, and `check` in
it is already correct and is compared byte for byte on submission, so never retype, reformat, or
shorten one. Edit only these, with the file tool:

- each observation: `verdict` (`supported`, `unsupported`, or `uncertain`), a nonempty `rationale`,
  and `sources`;
- each entry in `checks`: `status` (`pass`, `revise`, or `not_applicable`) and a nonempty
  `rationale`. All five of `estimates`, `scope`, `harms`, `overlap`, and `certainty` must be
  present, even when one is `not_applicable`;
- the record `status`: `pass` or `revise`;
- each entry in `membership_assessments`: `supported_scope` (`outcome`, `review`, `unsupported`, or
  `unknown`), a nonempty `rationale`, and `sources`.

A report group has `report_reviews` instead of one `record`: one entry per target, each with its own
`status` and observations.

## Rules the submission enforces

- An `unsupported` or `uncertain` verdict requires that check's `status` to be `revise` and the
  record `status` to be `revise`. A record with any `revise` check cannot be `pass`.
- A membership you did not verify requires the `overlap` check and the record to be `revise`.
- A source is `{document_id, locator, quote}`. Take the id and locator from `source_find` and the
  quote verbatim from `source_read`; the quote must occur at that locator. Cite only documents in
  that target's `allowed_document_ids`.
- Only a `scope` target may rest on metadata alone. Every other target needs a real document.
- The finding's `evidence` list sits once in the packet's `audit_group`, not inside the `harms` and
  `certainty` assertions. Judge both against it there.

Work through the targets in order and edit the proposal as you go. Then run the exact `submit`
command. If it reports problems, fix only the fields it names and submit again. On an unrecoverable
error run the exact `fail` command. Return only the `run_id`, `task_id`, and recorded state.
