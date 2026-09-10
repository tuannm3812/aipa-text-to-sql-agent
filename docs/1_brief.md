# Brief

**What:** A decision-support agent that answers plain-English questions about a
relational database by generating a single read-only SQLite `SELECT`, executing
it locally, and rendering the result.

**For whom:** A non-technical analyst who knows the business question but not the
schema, and a reviewer assessing whether an LLM can be trusted near a database.

**Done looks like:** A question in, a correct result table out, with the
generated SQL always visible and the retrieved schema explainable. The LLM never
receives row data — only DDL, column names, and low-cardinality value hints.

**Explicit non-goals:** No write path, ever. No cloud database. No fine-tuning.
No hiding the SQL from the user.

**Constraints:**
- SQLite only through Phase 2; the engine abstraction arrives in Phase 3.
- Two LLM backends: the Gemini API with multi-key failover, and local Ollama.
- The hosted Streamlit demo must stay within Community Cloud's free resources,
  which is why heavyweight embedding models are an optional dependency group
  rather than a runtime requirement.

Timestamped 2026-09-11. See `docs/superpowers/specs/2026-09-10-refactor-roadmap.md`
for the phased plan and `docs/4_next_steps.md` for what remains.
