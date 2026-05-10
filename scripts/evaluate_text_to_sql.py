from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import text_to_sql_agent_mvp as agent


def _normalise_rows(rows: list[tuple[Any, ...]]) -> list[list[str]]:
    return [[str(value) for value in row] for row in rows]


def _run_gold(case: dict[str, Any]) -> tuple[str, agent.QueryResult]:
    sql = case["gold_sql"]
    if not agent.is_safe_query(sql):
        return sql, agent.QueryResult(columns=[], rows=[], sql=sql, error="GOLD_SQL_UNSAFE")
    return sql, agent.execute_query(case["db_path"], sql)


def _run_llm(
    case: dict[str, Any],
    *,
    provider: str,
    model_name: str,
    use_rag: bool,
    rag_top_k: int,
) -> tuple[str, agent.QueryResult]:
    return agent.ask_database_with_sql(
        case["question"],
        db_path=case["db_path"],
        model_name=model_name,
        provider=provider,
        use_rag=use_rag,
        rag_top_k=rag_top_k,
    )


def evaluate_case(
    case: dict[str, Any],
    *,
    mode: str,
    provider: str,
    model_name: str,
    use_rag: bool,
    rag_top_k: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    gold_sql, gold_result = _run_gold(case)

    if mode == "gold":
        generated_sql, result = gold_sql, gold_result
    else:
        generated_sql, result = _run_llm(
            case,
            provider=provider,
            model_name=model_name,
            use_rag=use_rag,
            rag_top_k=rag_top_k,
        )

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    generated_rows = _normalise_rows(result.rows)
    gold_rows = _normalise_rows(gold_result.rows)
    exact_result_match = (
        result.error is None
        and gold_result.error is None
        and result.columns == gold_result.columns
        and generated_rows == gold_rows
    )

    return {
        "id": case["id"],
        "dataset": case["dataset"],
        "difficulty": case["difficulty"],
        "mode": mode,
        "provider": provider if mode == "llm" else "gold",
        "model_name": model_name if mode == "llm" else "gold_sql",
        "use_rag": use_rag if mode == "llm" else "",
        "rag_top_k": rag_top_k if mode == "llm" else "",
        "safe_sql": agent.is_safe_query(generated_sql),
        "execution_ok": result.error is None,
        "exact_result_match": exact_result_match,
        "latency_ms": latency_ms,
        "error": result.error or "",
        "generated_sql": generated_sql,
        "gold_sql": gold_sql,
    }


def write_markdown(rows: list[dict[str, Any]], output_path: Path) -> None:
    total = len(rows)
    exact = sum(1 for row in rows if row["exact_result_match"])
    safe = sum(1 for row in rows if row["safe_sql"])
    executed = sum(1 for row in rows if row["execution_ok"])
    avg_latency = round(sum(float(row["latency_ms"]) for row in rows) / total, 2) if total else 0

    lines = [
        "# Text-to-SQL Evaluation Results",
        "",
        f"- Cases: {total}",
        f"- Safe SQL rate: {safe}/{total}",
        f"- Execution success rate: {executed}/{total}",
        f"- Exact result match: {exact}/{total}",
        f"- Average latency: {avg_latency} ms",
        "",
        "| Case | Dataset | Difficulty | Safe | Executed | Exact Match | Latency ms |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {id} | {dataset} | {difficulty} | {safe_sql} | {execution_ok} | "
            "{exact_result_match} | {latency_ms} |".format(**row)
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Text-to-SQL generation against gold SQL results.")
    parser.add_argument("--cases", default="evaluation/cases.json")
    parser.add_argument("--mode", choices=["gold", "llm"], default="gold")
    parser.add_argument("--provider", choices=["gemini", "ollama"], default=agent.DEFAULT_PROVIDER)
    parser.add_argument("--model", default=agent.DEFAULT_MODEL_NAME)
    parser.add_argument("--no-rag", action="store_true")
    parser.add_argument("--rag-top-k", type=int, default=agent.DEFAULT_RAG_TOP_K)
    parser.add_argument("--out-dir", default="evaluation/results")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [
        evaluate_case(
            case,
            mode=args.mode,
            provider=args.provider,
            model_name=args.model,
            use_rag=not args.no_rag,
            rag_top_k=args.rag_top_k,
        )
        for case in cases
    ]

    csv_path = out_dir / f"evaluation_{args.mode}.csv"
    md_path = out_dir / f"evaluation_{args.mode}.md"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    write_markdown(rows, md_path)

    exact = sum(1 for row in rows if row["exact_result_match"])
    print(f"Evaluated {len(rows)} cases. Exact result match: {exact}/{len(rows)}")
    print(f"Wrote {csv_path} and {md_path}")


if __name__ == "__main__":
    main()
