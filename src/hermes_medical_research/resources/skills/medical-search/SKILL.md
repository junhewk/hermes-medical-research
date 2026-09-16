---
name: medical-search
description: Perform one deterministic medical-search task through the mdr CLI.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical search

For cron work, run `mdr --actor mdr-searcher search claim`. If it returns `state: idle`, return
`[SILENT]`. Otherwise inspect the returned packet and proposal at most once. Modify only that
proposal, and only when its bounded source or sensitivity choice is clearly wrong. Preserve a
prefilled frozen plan exactly on refresh or retry Cycles. Run the exact returned `execute` command;
it submits a first plan or resumes the frozen plan and performs retrieval deterministically. If
review-strategy approval or all-results confirmation is requested, pass only the exact returned
digest or token. On the first unrecoverable CLI error, run the returned `fail` command immediately;
do not inspect internal manifests, write diagnostic scripts, or retry alternate commands. Return
only the `run_id`, `task_id`, and recorded state. Never edit run artifacts or pass corpus text in Bot
messages.
