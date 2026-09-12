# Protocol and search design

Use schema `3` for new research questions. Existing schema `1`/`2` PICO/PCC questions are accepted
and normalized on initialization. Each component contains labeled `groups`; terms in a group
are synonyms (OR), distinct groups are separate concepts (AND).

| Framework | Required components | Optional components | Default search components |
|---|---|---|---|
| PICO | population, intervention | comparison, outcome, timepoint | population, intervention |
| PECO | population, exposure | comparison, outcome, timepoint | population, exposure |
| DIAGNOSTIC | population, index_test, target_condition | reference_standard | index_test, target_condition |
| PROGNOSIS | population | prognostic_factor, prediction_model, outcome, timepoint | population and supplied factor/model |
| PCC | population, concept | context | population, concept |

Use `search_components` to explicitly choose necessary retrieval components; explain omissions
and additions in `search_rationale`. All components remain available for eligibility and synthesis.
Avoid making every PICO field mandatory in retrieval. Keep validated study-design filters as
separate, explicitly justified strategies, including their source and version in the rationale.

Example input (illustrative research scope, not a clinical recommendation):

```json
{
  "schema_version": "3",
  "framework": "PICO",
  "question": "In adults with hypertension, how does exercise affect systolic blood pressure?",
  "components": {
    "population": {"groups": [{"label": "condition", "text": "hypertension", "synonyms": ["high blood pressure"], "candidate_mesh": ["Hypertension"]}]},
    "intervention": {"groups": [{"label": "activity", "text": "exercise", "synonyms": ["physical activity"], "candidate_mesh": ["Exercise"]}]},
    "outcome": {"groups": [{"label": "blood-pressure", "text": "systolic blood pressure"}]}
  },
  "search_components": ["population", "intervention"],
  "filters": {},
  "eligibility": {"include": ["Adults with hypertension", "Exercise compared with usual care or no structured exercise"], "exclude": ["Animal studies"]},
  "outcomes": ["Systolic blood pressure", "Adverse events"],
  "search_rationale": "Search population and intervention sensitively; assess blood-pressure outcomes and adult populations during screening."
}
```

`research init` writes a normalized `question.json` for child searches and an immutable protocol
in `research.json`. Report budgets default to 100 raw retrieved records per source and 30 unique
records attempted for full text. Retries do not consume a second full-text slot. Search plans reserve
capacity; attachment replaces the reservation with actual retrieved counts, including filtered records.
To revise protocol scope, eligibility, sources, or budgets, initialize a new workspace.

Default sources: PubMed, PMC, Europe PMC, OpenAlex, Semantic Scholar; clinical frameworks also add
ClinicalTrials.gov. Configured Scopus is included unless excluded. Explicit `sources` overrides this.
Use `NCBI_EMAIL`, optional `NCBI_API_KEY`, `OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`/`S2_API_KEY`,
and `SCOPUS_API_KEY`/`SCOPUS_INSTTOKEN`. Unpaywall uses `NCBI_EMAIL`. Keys belong in the host environment.

ClinicalTrials.gov registrations have no equivalent publication-date/language/type filters. Create a
separate undated registry question with the same original question. Registrations with posted results
and publications remain separate records linked through the study stage. Cochrane reviews found via
PubMed are not a direct search of CENTRAL or the Cochrane Library. Crossref supplies DOI metadata.

Sources and rationale: [Cochrane Handbook chapter 4](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-04),
[PubMed help](https://pubmed.ncbi.nlm.nih.gov/help/),
[Europe PMC API](https://europepmc.org/RestfulWebService),
[ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/about-api).
