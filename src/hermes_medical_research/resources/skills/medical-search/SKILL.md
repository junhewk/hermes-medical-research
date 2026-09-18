---
name: medical-search
description: Perform one deterministic medical-search task through the hmr CLI.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical search

You work on exactly one claimed search task. A runner gives you an instruction file with `run_id`,
`task_id`, `packet_path`, `proposal_path`, and exact commands. Without one, run
`hmr --actor hmr-searcher search claim`; if it returns `state: idle`, return `[SILENT]`.

Inspect the packet and proposal at most once. Modify only that proposal, and only when its bounded
source or sensitivity choice is clearly wrong. Preserve a prefilled frozen plan exactly on refresh or
retry Cycles. Run the exact `execute` command; it submits a first plan or resumes the frozen plan and
performs retrieval deterministically. If review-strategy approval or all-results confirmation is
requested, pass only the exact returned digest or token. On the first unrecoverable CLI error, run
the exact `fail` command immediately; do not inspect internal manifests, write diagnostic scripts, or
retry alternate commands. Return only the `run_id`, `task_id`, and recorded state. Never edit run
artifacts or skills, and never pass corpus text in Bot messages.
