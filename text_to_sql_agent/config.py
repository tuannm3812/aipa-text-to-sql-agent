"""Default settings and lookup tables shared across the agent package.

Holds model/provider defaults, execution and RAG tuning constants, and the
synonym and stopword tables used by `text_to_sql_agent.rag` when scoring
schema chunks against a question.
"""

DEFAULT_MODEL_NAME = "gemini-2.5-flash"
DEFAULT_OLLAMA_MODEL = "gemma3"
DEFAULT_PROVIDER = "gemini"
DEFAULT_MAX_ROWS = 1_000
# SQLite virtual-machine steps a single query may run before it is aborted.
# This is a runaway-query guard for the hosted demo, where anyone can submit a
# query: a cross join over a large table would otherwise run unbounded. All 12
# gold evaluation cases complete well inside it. 0 disables the guard. This is
# SQLiteEngine's own `default_work_limit` - a VM-instruction count, not a time
# budget, and specific to SQLite's `work_limit` unit alone. See
# DEFAULT_WORK_LIMIT_MS for the millisecond engines (DuckDB, PostgreSQL); do
# not pass this constant to either of them - `execution.execute_query` used
# to do exactly that (a ~100-second timeout instead of the intended 5), which
# is the bug `Engine.default_work_limit` fixes.
DEFAULT_MAX_VM_STEPS = 100_000
# Milliseconds a single query may run on a server engine (DuckDB, PostgreSQL)
# before it is aborted - each engine's own `default_work_limit`. A wall-clock
# budget, not an instruction count, which is why it is a separate constant
# from DEFAULT_MAX_VM_STEPS rather than the same number reused across units.
# Matches the design's QUERY_ABORTED_AFTER_5000_MS
# (docs/superpowers/specs/2026-09-14-phase-3-engine-abstraction-design.md §4.5).
DEFAULT_WORK_LIMIT_MS = 5_000
DEFAULT_RAG_TOP_K = 6
DEFAULT_RAG_NEIGHBORS = 1
DEFAULT_RAG_SEMANTIC_WEIGHT = 3.0
DEFAULT_RAG_EMBEDDING_WEIGHT = 5.0
DEFAULT_VALUE_HINT_LIMIT = 6
DEFAULT_VALUE_HINT_MAX_CARDINALITY = 20

RAG_SYNONYMS = {
    "client": ["customer", "customers"],
    "clients": ["customer", "customers"],
    "buyer": ["customer", "customers"],
    "buyers": ["customer", "customers"],
    "income": ["revenue", "amount", "sales"],
    "revenue": ["amount", "sales", "total"],
    "sale": ["sales", "amount"],
    "sales": ["sale", "amount", "revenue"],
    "spend": ["amount", "sales"],
    "course": ["courses", "class"],
    "classes": ["courses", "course"],
    "student": ["students", "learner"],
    "patients": ["patient", "healthcare"],
    "patient": ["patients", "healthcare"],
    "region": ["location", "area"],
}

# fmt: off
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "how", "in",
    "is", "it", "me", "of", "on", "or", "per", "show", "the", "to", "total",
    "what", "which", "with",
}
# fmt: on
