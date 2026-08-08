# Question schema

Emit schema version `"2"`. The CLI accepts schema-v1 questions and wraps each legacy component in
one group, but review and store the normalized v2 artifact.

## Group semantics

Every framework component contains one or more labeled groups:

```json
{
  "groups": [
    {
      "label": "technology",
      "text": "large language models",
      "synonyms": ["generative artificial intelligence"],
      "candidate_mesh": ["Artificial Intelligence"]
    },
    {
      "label": "training focus",
      "text": "communication skills training",
      "synonyms": ["clinical communication training"],
      "candidate_mesh": ["Communication"]
    }
  ]
}
```

- AND groups within a component; OR the canonical text, synonyms, and resolved MeSH within a group.
- Require non-empty group labels and text. Keep labels unique within a component.
- Put only interchangeable terms in `synonyms`; create another group for a separately required idea.
- Omit ambiguous standalone acronyms such as `LLM` unless explicitly supplied or approved.
- Leave `resolved_mesh` empty. The CLI populates it after validating `candidate_mesh` against NCBI.

## Compound PCC

Use Population and Concept as required components. Reserve optional Context for setting,
environment, geography, or care/education setting:

```json
{
  "schema_version": "2",
  "framework": "PCC",
  "question": "How are large language models used for communication-skills training in undergraduate medical students?",
  "components": {
    "population": {
      "groups": [
        {"label": "learners", "text": "undergraduate medical students", "synonyms": ["medical students"]}
      ]
    },
    "concept": {
      "groups": [
        {"label": "technology", "text": "large language models", "synonyms": ["generative artificial intelligence"]},
        {"label": "training focus", "text": "communication skills training", "synonyms": ["clinical communication training"]}
      ]
    },
    "context": {
      "groups": [
        {"label": "setting", "text": "undergraduate medical education", "synonyms": ["medical school curriculum"]}
      ]
    }
  },
  "filters": {},
  "sources": ["pubmed", "pmc", "openalex", "semantic-scholar"],
  "exclude_sources": []
}
```

The sensitivity query is Population AND Technology AND Training-focus. The precision query adds
Context. Never place communication training in Context merely to create an AND condition.

## PICO

Require Population and Intervention. Allow optional Comparison and Outcome. Any component may
contain multiple required groups, such as an adult group AND a disease group under Population.

Top-level `sources` explicitly selects providers; `exclude_sources` removes providers from automatic
selection. Valid providers are `pubmed`, `pmc`, `openalex`, `semantic-scholar`, and `scopus`.

Filters accept `from_date`, `to_date`, `languages`, and `publication_types`. Dates use `YYYY-MM-DD`.
Review mode adds no date bound; quick mode adds a three-year `from_date` only when none is supplied.
