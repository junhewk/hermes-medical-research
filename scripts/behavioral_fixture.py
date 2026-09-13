"""Create an isolated, explicitly synthetic corpus for host workflow acceptance tests.

Contains no real patients, real publications, API credentials, or expected model answer.
Run with the candidate CLI environment, then give the host the skill and generated run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from xml.sax.saxutils import escape

from hermes_medical_search.artifacts import RunStore
from hermes_medical_search.config import Credentials
from hermes_medical_search.models import Question
from hermes_medical_search.query import compile_strategy
from medical_deep_research_plugin.fulltext import parse_jats
from medical_deep_research_plugin.workspace import Workspace, now

TRIAL_METHODS = (
    "Participants were randomized using a computer-generated sequence. An independent service "
    "concealed allocations until enrollment. Participants knew their assigned exercise, but "
    "outcome assessors were blinded. All randomized participants were included in the assigned "
    "group analysis, with no missing outcome observations. Both groups received the assigned "
    "interventions without reported deviations. The blood-pressure outcome and analysis at "
    "12 weeks were specified in the prospectively registered protocol; all specified outcomes "
    "are reported here."
)
REVIEW_METHODS = (
    "Eligibility was prespecified: randomized trials in adults with hypertension comparing "
    "structured exercise with inactive control. MEDLINE and CENTRAL were searched from inception; "
    "trial registries, references, and unpublished reports were also checked without language "
    "restriction. Two reviewers independently selected studies, extracted data, and assessed "
    "risk of bias. Disagreements were adjudicated. All eligible studies were included in the "
    "analysis using the prespecified random-effects model, with sensitivity analyses for risk "
    "of bias. No selective omission was found."
)
CORPUS = [
    (
        "R1",
        "SYNTHETIC review of exercise versus inactive control in adult hypertension",
        "systematic-review",
        REVIEW_METHODS,
        "Two trials, T1 and T3, were included. The pooled between-group office SBP mean difference "
        "was -4.0 mmHg (95% confidence interval -6.0 to -2.0), and DBP -2.0 mmHg "
        "(95% confidence interval -3.0 to -1.0). The source authors rated SBP certainty moderate "
        "because of imprecision. Safety event counts were not collected by this review.",
    ),
    (
        "R2",
        "SYNTHETIC updated network review of exercise modalities",
        "systematic-review",
        REVIEW_METHODS,
        "The network includes trials T1 and T3 and one additional small resistance trial. The "
        "between-group office SBP estimate for aerobic exercise versus "
        "inactive control is -4.2 mmHg "
        "(95% credible interval -7.0 to -1.0). Isometric exercise ranks first with SUCRA 92%, "
        "but exercise-versus-exercise comparisons are inconclusive. Trial overlap with review R1 "
        "includes both T1 and T3. No ambulatory BP analysis or comparative "
        "harms estimate is available.",
    ),
    (
        "T1",
        "SYNTHETIC aerobic exercise versus usual care randomized trial",
        "primary",
        TRIAL_METHODS,
        "Eighty adults with hypertension were randomized equally to aerobic "
        "exercise or usual care. "
        "At 12 weeks the between-group office SBP mean difference was -5.0 mmHg (95% confidence "
        "interval -8.0 to -2.0), and DBP -2.0 mmHg (95% confidence interval -4.0 to 0.0). "
        "Two of 40 exercise participants and one of 40 usual-care "
        "participants reported mild muscle "
        "soreness. No serious adverse event was observed in either arm during 12 weeks; the trial "
        "was not powered to compare rare harms.",
    ),
    (
        "T2",
        "SYNTHETIC combined exercise versus health education trial",
        "primary",
        TRIAL_METHODS,
        "One hundred sixty older adults with treated hypertension were randomized to combined "
        "exercise or health education. At 12 weeks, final 24-hour ambulatory SBP means were "
        "129.1 and 128.9 mmHg, respectively; baseline means were 128.6 and 128.4 mmHg. The "
        "group-by-time test had P=0.98. An adjusted between-group effect and confidence interval "
        "were not reported. The exercise-minus-education peak oxygen consumption difference "
        "was 0.4 mL/kg/min (95% confidence interval -0.5 to 1.3). Adverse "
        "events were not reported.",
    ),
    (
        "T3",
        "SYNTHETIC home handgrip versus inactive control trial",
        "primary",
        TRIAL_METHODS,
        "Thirty older adults with hypertension were randomized, 15 per arm. "
        "At eight weeks, the within-exercise-arm "
        "SBP mean change was -7.3 mmHg (95% confidence interval -12.2 to "
        "-2.4), and the within-control-arm "
        "change was -0.1 mmHg (95% confidence interval -6.1 to 5.9). The "
        "between-group test had P=0.03; "
        "a between-group effect interval was not reported. Biweekly calls "
        "monitored adverse events, "
        "but event counts were not reported. This trial used the prespecified eight-week endpoint "
        "instead of the twelve-week endpoint described in the generic methods summary above.",
    ),
    (
        "T4",
        "SYNTHETIC aerobic exercise versus heat therapy randomized trial",
        "primary",
        TRIAL_METHODS,
        "Forty-two adults with untreated elevated blood pressure were "
        "randomized to aerobic exercise "
        "or hot-water immersion; there was no inactive control. Neither intervention showed a "
        "detectable change in 24-hour ambulatory SBP over ten weeks (time "
        "P=0.86). Heat therapy was "
        "not significantly different from exercise (interaction P=0.64). One"
        " post-immersion syncope "
        "event occurred in the heat-therapy arm; its seriousness classification and exercise-arm "
        "event counts were not reported. The endpoint was ten weeks, despite twelve weeks being "
        "stated in the generic methods summary above.",
    ),
    (
        "X1",
        "SYNTHETIC exercise in children with hypertension",
        "primary",
        "This study enrolled children aged 8-12 years.",
        "Childhood blood pressure was measured.",
    ),
    (
        "X2",
        "SYNTHETIC exercise physiology in rats",
        "other",
        "Laboratory rat experiment.",
        "Rat blood pressure was measured.",
    ),
]


def create(output: Path) -> dict:
    workspace = Workspace(output / "run")
    question = {
        "schema_version": "3",
        "framework": "PICO",
        "question": "In adults with hypertension, what are the blood-pressure effects "
        "and harms of structured exercise?",
        "components": {
            "population": {"groups": [{"label": "condition", "text": "hypertension"}]},
            "intervention": {"groups": [{"label": "activity", "text": "exercise"}]},
        },
        "sources": ["europe-pmc"],
        "eligibility": {
            "include": [
                "Adults with hypertension; exercise trials or systematic reviews.",
                "Active-comparator reports may contribute separately labeled context.",
            ],
            "exclude": ["Children", "Animal studies"],
        },
        "outcomes": [
            "Office systolic blood pressure",
            "Office diastolic blood pressure",
            "24-hour ambulatory blood pressure",
            "Adverse events",
        ],
        "search_rationale": "SIMULATED WORKFLOW TEST ONLY. Frozen synthetic source corpus "
        "replaces live retrieval; no clinical conclusions or coverage claims"
        " about real literature are justified.",
    }
    workspace.init(question, mode="report", records=100, fulltexts=30, language="en")
    q = Question.from_dict(workspace.load()["protocol"]["question"])
    strategy = compile_strategy(q, mode="quick", limit_per_source=8, sources=["europe-pmc"])
    store = RunStore(output / "search")
    manifest = store.initialize(q, strategy, Credentials())
    records = [
        {
            "source": "europe-pmc",
            "source_id": code,
            "title": title,
            "abstract": results,
            "authors": ["Synthetic Fixture Authors"],
            "year": "2025",
            "publication_types": [
                "Systematic Review" if kind == "systematic-review" else "Randomized trial"
            ],
            "retrieved_at": now(),
            "url": f"https://example.org/mdr-synthetic/{code}",
        }
        for code, title, kind, methods, results in CORPUS
    ]
    store.append_source("europe-pmc", records)
    manifest["status"] = "complete"
    manifest["sources"]["europe-pmc"].update(
        status="complete", retrieved=8, retained=8, reported_total=8, truncated=False
    )
    store.write_manifest(manifest)
    workspace.attach(store.path)
    docs = workspace.rows("documents")
    research = workspace.load()
    for _code, title, _kind, methods, results in CORPUS:
        record = next(r for r in workspace.rows("records") if r["title"] == title)
        raw = (
            "<article><body><sec><title>Methods</title><p>"
            + escape(methods)
            + "</p></sec><sec><title>Results</title><p>"
            + escape(results)
            + "</p></sec></body></article>"
        ).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        name = f"fulltext/{checksum}.xml"
        (workspace.path / name).parent.mkdir(exist_ok=True)
        (workspace.path / name).write_bytes(raw)
        docs.append(
            {
                "document_id": record["record_id"] + ":fulltext",
                "record_id": record["record_id"],
                "kind": "fulltext",
                "file": name,
                "sha256": checksum,
                "segments": parse_jats(raw),
                "url": record["url"],
                "retrieved_at": now(),
            }
        )
        research["fulltext_attempts"][record["record_id"]] = {
            "status": "available",
            "started_at": now(),
        }
    workspace.save(research)
    workspace.put("documents", {"schema_version": "1", "records": docs})
    return {"run_dir": str(workspace.path), "records": 8, "simulation": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(create(args.output), indent=2))
