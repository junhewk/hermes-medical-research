# ADR 0001: Hermes skills over a deterministic CLI

- Status: accepted; Bot-to-Bot routing superseded in part by ADR 0002
- Date: 2026-09-15

## Context

The previous design coupled report authors to host-native plugin hooks, private delegated-agent APIs,
large review packets, and host-specific budget accounting. Those mechanisms were difficult to expose
consistently to delegated children and could not themselves guarantee corpus immutability, complete
target coverage, valid citations, reviewer independence, stale-result rejection, or legal workflow
transitions.

Hermes skills are appropriate when a capability can be expressed as instructions plus terminal/file
commands. Hermes Bot profiles provide persistent, isolated roles. The repository already has a
strong Python evidence model and validation layer.

## Decision

Ship `hermes-medical-research` as a normal Python package with one executable, `mdr`. Provide four
small Hermes skills and six named profiles. Bots exchange only opaque Run and Task IDs. The CLI reads
the corpus, creates bounded packets and proposal templates, validates submissions, writes immutable
receipts, routes revisions, verifies citations, and finalizes reports.

Hermes bootstrap uses only the public profile CLI and checked-in profile/skill files. It is dry-run by
default, copies only model/provider/timezone selection from the invoking configuration, grants only
terminal, file, and skills toolsets, and refuses unmanaged changes. Bot Mode discovers these profiles
through the connected Hermes gateway. ADR 0002 replaces direct Bot-to-Bot routing with cron claims.

## Consequences

- Host plugin manifests, lifecycle hooks, private child construction, and the host-native adapter are
  removed.
- Task packets remain bounded; no reviewer owns the whole report as one free-form job.
- Proposals remain editable for agent ergonomics, but have no authority until deterministic validation
  produces a digest-bound receipt.
- Audit revisions go back to a non-auditor specialist, preserving role independence.
- v0.4 migration is copy-on-import and archives prior review/completion artifacts as provenance.
- A selector terminal pilot is the first operational gate. Three clean full runs are required before
  the GitHub repository is renamed.
