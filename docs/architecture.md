# Architecture Diagram

The draw.io source file is available at:

```text
docs/architecture.drawio
```

Open it with [diagrams.net](https://app.diagrams.net/) and export it as PNG or PDF for the final report and slide deck.

## Diagram Caption

The Enterprise Text-to-SQL Agent grounds LLM SQL generation using hybrid schema RAG, validates generated SQL through deterministic and AST-based safety checks, executes only read-only SQLite queries, and returns results with generated SQL and retrieval evidence.

## Recommended Placement

- Report: Workflow and Methodology section.
- Presentation: System Workflow slide.
