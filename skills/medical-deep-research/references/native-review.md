# Native review and shared budget

A report requires a fresh reviewer task through the current host. Both roles inherit the
host's existing model and authentication. This plugin has no model API client, API-key
wizard, or second provider configuration. Native hooks/tools must actually be available;
a copied skill alone cannot provide this integration. If they are unavailable, report the
missing capability and keep the checkpoint. Do not replace the reviewer with self-review.

## Start before research

Use one report directory and one author session. The default total is 150. Report searches,
source reads, assessment, all reviewer attempts, corrections and delivery share that total.
A resumed report retains its remaining allowance. Changing the total silently is rejected.

**Hermes 0.21.2:** call `medical_research_session` with `run_dir` and `total_turns: 150` before
initializing/researching. The tool binds the live author's native iteration counter. Code
execution refunds are disabled for this report, so code-mode loops still consume iterations.
Review iterations are reserved before delegation and deducted from the author's remaining
cap after measured completion. The host's final toolless grace reply is outside that counter.
The adapter changes only the active report session, not gateway configuration or global caps.
The reviewer inherits the live parent output-token limit, or the host's existing
`model.max_tokens` value when the parent did not propagate it. No new limit or model is chosen.

**Codex / Claude Code:** run this as one terminal command from the host:

```bash
medical-deep-research-plugin research host-session RUN --total-turns 150
```

Plugin hooks bind the actual host session and confirm activation. If hooks are disabled or
untrusted, this command fails instead of pretending the budget is enforced. Codex requires
trusting the installed plugin hooks using its normal host controls. For tests, configure a
separate plugin data directory; production uses the host's plugin data storage.

These hosts conservatively charge each local tool invocation as one unit, including each
reviewer read/write and native delegation call. Parallel tool invocations are charged
separately. Use the plugin CLI for retrieval and file/terminal tools for evidence work.
Hosted web tools and transport polls are outside this hook counter and are not part of this
workflow. This is a bound on the supported tool workflow, not on provider retries, tokens,
API requests or ordinary toolless responses. `native-review.json` and `completion.json`
state the actual unit, author usage, reviewer usage/reservations and remaining total.

## Delegate after synthesis

Aim to finish optional expansion by unit 100. Keep at least 25 units available for review
and finalization; a correction and another review also consume the same total. All findings
and the entire stored corpus are frozen for review, including records that might contradict
claims that a modality, trial, outcome or harm is absent. Prior reviews and author chat
history are omitted. The reviewer can read frozen sources and write only its assigned
result file. Returning long JSON in a chat message is unreliable because hosts truncate it.

- Hermes: call `medical_research_review` with `run_dir`, `action: "start"`, `review_turns: 20`.
  It starts a native child asynchronously. Call the same tool with `action: "status"` and
  `wait_seconds: 45` until terminal. These bounded waits avoid the host's tool timeout.
- Claude Code: call `Agent` with
  `subagent_type: "medical-deep-research-plugin:medical-evidence-reviewer"`, synchronously.
  The agent inherits the model, has Read/Write access to its assigned files, and a 20-turn cap.
- Codex: spawn a native reviewer with the host's fresh-context option explicitly set
  (`fork_context: false`, or `fork_turns: "none"` where exposed). Name/describe the task as
  medical evidence review. The hook supplies its frozen packet and reserves 20 units.
  Wait for the task. Do not fork the author's history, select another model or delegate further.

The native adapter records the returned file and host task identity. The author may read
required revisions but cannot approve itself or edit the recorded verdict. A valid `revise`
verdict is useful progress: correct its evidence/synthesis claims, then delegate another
fresh review. Never repair a claim only inside the review rationale. Finalization requires
passing verdicts for every finding against the current complete candidate digest.

A missing output, invalid quote/schema, truncation, unknown child termination or exhausted
budget cannot become a passing review. Unknown termination retains its reservation. Do not
retry a still-running task, reset the budget, or manually manufacture a native receipt.
All machine checks establish traceability/consistency; a separate model can still make
factual errors. Behavioral qualification and transparent report limitations remain necessary.
