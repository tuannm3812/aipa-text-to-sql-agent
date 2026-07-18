# Architecture Notes

The draw.io source file is available at:

```text
docs/supporting/architecture.drawio
```

Open it with [diagrams.net](https://app.diagrams.net/) and export it as PNG or PDF for the final report and slide deck.

Two other renderings of the same "User Tool Workflow" diagram exist for different purposes:

- `docs/supporting/architecture-workflow.html` - a hand-built HTML/SVG page (open it in any browser) styled as a dark circuit schematic. `docs/supporting/screenshots/00-architecture-workflow.png` is its export, and is what `README.md` actually embeds.
- `docs/supporting/architecture-workflow.excalidraw` - the same diagram in [Excalidraw](https://excalidraw.com)'s hand-drawn sketch style. Open it at excalidraw.com (`File -> Open`) or in the Excalidraw VS Code extension to view or restyle it further.

All three describe the identical flow (same boxes, same non-crossing two-row layout); keep them in sync if the runtime flow changes.

## Current Implementation

The current project is an end-to-end Text-to-SQL decision support prototype. A user selects a SQLite database or uploads CSV files in Streamlit, asks a natural-language question, and receives generated SQL plus a local query result table.

The implemented backend flow is:

1. Extract SQLite schema metadata and foreign-key relationships.
2. Build table-level schema chunks with columns, DDL, relationship data, and low-cardinality value hints.
3. Retrieve relevant schema using hybrid lexical, synonym, character n-gram, hashed embedding, value-hint, and graph-neighbour signals.
4. Generate one SQLite query with Gemini or a local Ollama model.
5. Validate that the query is read-only with deterministic checks and optional `sqlglot` AST parsing.
6. Execute through a read-only SQLite connection with `PRAGMA query_only` and an authorizer.
7. Display generated SQL, result rows, selected schema context, and retrieval diagnostics in Streamlit, with an automatic bar chart when the result is a two-column category + number shape.

The local verification status as of this documentation pass is:

- Unit tests: `18` tests passing with `python3 -m unittest discover -s tests`.
- Gold evaluation: `12/12` safe, executed, value-matched, row-matched, and exact-matched cases with `python3 scripts/evaluate_text_to_sql.py --mode gold`.
- Gemini evaluation: `gemini-2.5-flash` completed all `12` cases with multi-key quota failover, reaching `11/12` value match.
- Local LLM evaluation: Ollama `llama3:latest` reached `8/12` value match with `12/12` safe/executed queries; `gemma4:latest` reached `8/12` value match overall and `8/10` among executed queries.

## Diagram Source

The file has four pages:

- `User Tool Workflow`: use this page as the main architecture figure. It shows only the live user path: question input, database connection, schema retrieval, SQL generation, safety validation, read-only execution, and answer display (including the optional auto bar chart). It carries small tech-stack marks (Streamlit, SQLite, Python, Gemini, Ollama) next to the box each technology belongs to, colored to match the badges already used in `README.md`.
- `Hybrid Schema RAG Detail`: use this page when explaining the retrieval method. It expands the Schema RAG internals into tokenisation, synonym expansion, query decomposition, schema chunking, retrieval signals, graph expansion, ranking, prompt context, and diagnostics.
- `Offline Evaluation Workflow`: use this page only in the empirical results section. It is deliberately separated from the user tool workflow because benchmark evaluation is a local validation process, not part of normal app usage.
- `Implementation Modules`: use this page if the teacher asks how the code is organised after the refactor.

Dashed boxes represent supporting or optional runtime behaviour, such as Gemini key failover or the one-attempt SQL repair path. They are not separate user actions.

The diagram is styled with Google Sans throughout (every cell across all four pages sets `fontFamily=Google Sans`). If the font is not available on the export machine, diagrams.net will fall back to the closest installed sans-serif font; the layout should still remain readable.

The tech-stack logos on the `User Tool Workflow` page are loaded from `cdn.simpleicons.org` by URL, not embedded in the file. diagrams.net needs internet access to fetch and render them when you open or export the diagram; if you need a fully offline copy, re-export the PNG once online and the raster copy will no longer need network access.

The `User Tool Workflow` page uses a two-row layout: a top row that flows left to right (ask the question, connect data, retrieve schema, generate SQL), and a bottom row that flows right to left (validate, execute, answer), joined by a single vertical connector where SQL generation hands off to the safety gate. Branch-only outcomes (blocked/unsafe, one-shot repair, auto chart) drop straight down from their trigger box instead of looping back across the diagram, which is what keeps the connector lines from crossing each other.

The `Hybrid Schema RAG Detail` page uses a method layout:

- Question normalisation and tokenisation
- Domain synonym expansion
- Query-intent decomposition
- Schema chunk cache
- Retrieval scoring signals
- Foreign-key graph expansion
- Ranked prompt context and retrieval diagnostics

## Export Guidance

In diagrams.net, open `docs/supporting/architecture.drawio`, choose the page tab at the bottom, then use `File -> Export as -> PNG` or `PDF`. For slides, export the `User Tool Workflow` page as a PNG with a transparent background disabled so it remains readable on a white slide.

Exports already exist for the report figures:

- `User Tool Workflow`: use `docs/supporting/screenshots/00-architecture-workflow.png` (the designed HTML/SVG version, not a draw.io export - see above) for the main workflow figure.
- `Hybrid Schema RAG Detail`: [`docs/supporting/architecture-rag-detail.png`](architecture-rag-detail.png) for the retrieval-method figure.
- `Offline Evaluation Workflow`: [`docs/supporting/architecture-evaluation-workflow.jpeg`](architecture-evaluation-workflow.jpeg) for the empirical-results figure.
- `Implementation Modules`: [`docs/supporting/architecture-implementation-modules.jpeg`](architecture-implementation-modules.jpeg) as backup evidence if asked how the refactored code maps to the system design.

Re-export a page only if its content in `architecture.drawio` changes; otherwise these four files stay current.

## Diagram Caption

The Enterprise Text-to-SQL Agent lets a user ask a natural-language question over a selected SQLite database or uploaded CSV-derived database, grounds SQL generation using hybrid schema RAG, validates the generated SQL candidate through deterministic and AST-based safety checks, executes only read-only SQLite queries, and returns results with generated SQL and retrieval evidence.

## Recommended Placement

- `User Tool Workflow`: report Section 4.1 and the system workflow slide.
- `Hybrid Schema RAG Detail`: report Section 4.3 or 4.4 when explaining retrieval.
- `Offline Evaluation Workflow`: report Section 5.1 or 5.4 when explaining benchmark evidence.
