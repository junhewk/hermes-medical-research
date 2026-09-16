"""Deterministic one-hop citation search planning."""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

from hermes_medical_research.search.artifacts import RunStore, strategy_digest
from hermes_medical_research.search.config import Credentials
from hermes_medical_research.search.models import Question, SourceStrategy, ValidationError
from hermes_medical_research.search.query import compile_strategy

from .workspace import Workspace


def plan_snowball(
    workspace: Workspace, record_id: str, direction: str, limit: int
) -> dict[str, Any]:
    records = workspace.index("records")
    if record_id not in records or not str(records[record_id].get("pmid") or "").isdigit():
        raise ValidationError("Europe PMC citation chaining requires a seed record with a PMID")
    if direction not in {"references", "citations"}:
        raise ValidationError("citation direction must be references or citations")
    if not 1 <= limit <= 100:
        raise ValidationError("one-hop citation limit must be between 1 and 100")
    protocol = workspace.load()["protocol"]
    question = Question.from_dict(protocol["question"])
    question.filters = type(question.filters)()
    mode = "review" if protocol["mode"] == "review-prep" else "quick"
    strategy = compile_strategy(
        question, mode=mode, limit_per_source=limit, sources=["europe-pmc"]
    )
    pmid = str(records[record_id]["pmid"])
    strategy.strategies["europe-pmc"] = SourceStrategy(
        source="europe-pmc",
        query=f"{direction}(MED:{pmid})",
        precision_query=None,
        selected_variant="sensitivity",
        request_parameters={"link_seed": pmid, "link_direction": direction},
        warnings=["One-hop citation traversal; apply protocol eligibility during screening."],
    )
    output = workspace.path / "searches" / ("citation-" + uuid4().hex[:12])
    workspace.reserve(output, strategy)
    store = RunStore(output)
    manifest = store.initialize(question, strategy, Credentials.from_env())
    manifest["research_parent"] = os.path.relpath(workspace.path, output)
    store.write_manifest(manifest)
    return {
        "run_dir": str(output),
        "strategy_digest": strategy_digest(strategy),
        "status": manifest["status"],
        "strategy": strategy.to_dict(),
    }
