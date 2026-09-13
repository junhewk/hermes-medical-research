# Appraisal and evidence weighting

Separate three decisions: retrieval priority, appraisal of a specific study result, and certainty
in a synthesis outcome. Citations, recency, journal reputation, and publication-type heuristics
can help prioritize reading; they cannot establish certainty or determine clinical conclusions.

| Evidence | CLI method / version | Guidance |
|---|---|---|
| Individually randomized parallel trials | rob2 / 2019-08-22 | [RoB 2](https://www.riskofbias.info/welcome/rob-2-0-tool) |
| Cluster / crossover trials | rob2-cluster or rob2-crossover / 2021-03-18 | Use the corresponding RoB 2 variant |
| Nonrandomized interventions | robins-i / 2016 | [ROBINS-I](https://www.riskofbias.info/welcome/home/current-version-of-robins-i) |
| Nonrandomized exposures | robins-e / 2024 | [ROBINS-E](https://www.riskofbias.info/welcome/robins-e-tool) |
| Diagnostic accuracy estimates | quadas3 / 1.2 | [QUADAS-3](https://www.bristol.ac.uk/population-health-sciences/projects/quadas/quadas-3/quadas-3-tool/) |
| Prognostic factors | quips / 2013 | [QUIPS](https://pubmed.ncbi.nlm.nih.gov/23420236/) |
| Prediction models | probast-ai / 2025 | [PROBAST+AI](https://www.probast.org/) |
| Systematic reviews used as central evidence | robis / 2016 | [ROBIS](https://www.bristol.ac.uk/population-health-sciences/projects/robis/) assesses review-level risk of bias; record eligibility, identification, data_collection and synthesis domains |
| Descriptive maps, reviews/guidelines used as context, or insufficient methods | descriptive / 1 | Record limitations and applicability; do not imply a formal instrument was completed |

These method identifiers route the agent to the cited instruments; the CLI checks the evidence
record structure and does not reproduce their full signaling questions or decision algorithms.
Use the named published versions, rather than silently switching to draft revisions. Preserve
attribution and consult the original instrument and its usage terms when applying it.

For each result assess internal validity and directness to the actual research question. Record
confounding, selection, missing data, measurement, reporting, applicability, and model validation
as relevant. Include quotations or table/page locations supporting assessed domains; use
`not_assessed` when the necessary methods/results are inaccessible. A full-text paper elsewhere
in the same finding does not make an abstract-only contributor fully assessed.

For quantitative clinical findings, use [Cochrane's GRADE guidance](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-14):
state the body-of-evidence starting point and reasons concerning risk of bias, inconsistency,
indirectness, imprecision, and publication bias. Explain any upgrading or downgrading without
double-counting the same concern. Use question-specific guidance for diagnostic/prognostic
evidence; do not mechanically apply an intervention hierarchy to every question.

Weight contributions through explicit methodological and applicability reasoning. Keep effect
measures, populations, comparisons, thresholds, and follow-up periods aligned. Different studies
can answer different questions without contradicting one another. Explain true disagreements;
do not select only studies that favor a preferred conclusion. Check systematic reviews for overlap
with included primary studies. Guidelines are recommendations with their own methods and context,
not an additional independent trial or an automatic source of high-certainty evidence.

All generated assessments remain provisional. A report's implications should follow its certainty
and applicability, with benefits, harms, uncertainty, and evidence gaps visible. This release
produces narrative synthesis and evidence maps; it does not calculate pooled effects.

Distinguish pending work from unavailable methods using the v2 completion/status fields. Inspect
stored full texts before asserting access is unavailable. Consult the instrument's published
signaling questions when applying it; do not call a domain-only record a fully implemented formal
instrument. Source-author certainty is attributed separately from this report's GRADE-informed
assessment. A descriptive appraisal alone does not establish low risk of bias for an effect estimate.
