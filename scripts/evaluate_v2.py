"""Run an evaluation suite under the v2 contract and write one result directory per suite.

    uv run python scripts/evaluate_v2.py --suite demo --mode gold
    uv run python scripts/evaluate_v2.py --suite spider_dev --subset subset200 \\
        --mode llm --provider ollama --model llama3:latest
    uv run python scripts/evaluate_v2.py --suite bird_dev --mode llm ... --resume DIR
    uv run python scripts/evaluate_v2.py --gate gold --suite demo safety

`--gate gold` runs each suite in gold mode into a temporary directory (or `--out-root`) and
judges it by spec §4.4: valid references self-match, every `reference_invalid` ID is on
`<suites-dir>/<suite>.gold_exceptions.txt`, non-answerable cases are well-formed.

Each run writes `<out-root>/<run id>/{manifest.json,cases.csv,report.md}`; nothing is ever
overwritten. `--resume DIR` continues an incomplete run in place and is refused - exit code
2, naming the fields - unless the same arguments reproduce the saved identity.

Exit codes: 0 every run complete (or every gate passed); 1 a run is incomplete (outages) or a
gate failed; 2 refused.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from text_to_sql_agent.config import (  # noqa: E402 - import must follow the sys.path insert
    DEFAULT_MODEL_NAME,
    DEFAULT_PROVIDER,
    DEFAULT_RAG_TOP_K,
)
from text_to_sql_agent.dsn import redact_dsn  # noqa: E402
from text_to_sql_agent.evaluation_v2.contract import SuiteError  # noqa: E402
from text_to_sql_agent.evaluation_v2.gates import GateResult, gold_gate  # noqa: E402
from text_to_sql_agent.evaluation_v2.runner import (  # noqa: E402
    ResumeRefused,
    RunConfig,
    run_suite,
    select_cases,
)

DEFAULT_SUITES_DIR = Path("evaluation/suites")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--suite", nargs="+", required=True, help="suite name(s), e.g. demo")
    parser.add_argument(
        "--subset", default="full", help="'full', or a subset name such as subset200"
    )
    parser.add_argument("--mode", choices=["gold", "llm"], default="gold")
    parser.add_argument("--provider", choices=["gemini", "ollama"], default=DEFAULT_PROVIDER)
    parser.add_argument("--model", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--no-rag", action="store_true")
    parser.add_argument("--rag-top-k", type=int, default=DEFAULT_RAG_TOP_K)
    parser.add_argument("--evidence", choices=["on", "off"], default="off")
    parser.add_argument("--max-retries", type=int, default=0)
    parser.add_argument("--retry-base-seconds", type=float, default=20.0)
    parser.add_argument(
        "--work-limit",
        type=int,
        default=None,
        help="execution budget in the engine's unit (SQLite: VM steps; 0 disables); "
        "default: the engine's demo guard",
    )
    parser.add_argument(
        "--max-rows", type=int, default=None, help="row cap; default: the app's DEFAULT_MAX_ROWS"
    )
    parser.add_argument("--resume", type=Path, metavar="DIR", default=None)
    parser.add_argument(
        "--gate",
        choices=["gold"],
        default=None,
        help="judge each suite's gold run instead of recording a result "
        "(output goes to a temporary directory unless --out-root is given)",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=None,
        help="where result directories are written (default: evaluation/results; "
        "with --gate, a temporary directory)",
    )
    parser.add_argument("--suites-dir", type=Path, default=DEFAULT_SUITES_DIR)
    return parser


def _config(args: argparse.Namespace, suite: str) -> RunConfig:
    subset_path = None if args.subset == "full" else args.suites_dir / f"{suite}.{args.subset}.txt"
    return RunConfig(
        suite=suite,
        suite_path=args.suites_dir / f"{suite}.jsonl",
        mode=args.mode,
        provider=args.provider,
        model=args.model,
        subset=args.subset,
        subset_path=subset_path,
        evidence=args.evidence == "on",
        use_rag=not args.no_rag,
        rag_top_k=args.rag_top_k,
        max_retries=args.max_retries,
        retry_base_seconds=args.retry_base_seconds,
        work_limit=args.work_limit,
        max_rows=args.max_rows,
    )


def _print_gate(result: GateResult) -> None:
    verdict = "PASS" if result.passed else "FAIL"
    print(f"{result.suite}: gold gate {verdict}")
    print(
        f"  {result.answerable} answerable, {result.non_answerable} non-answerable; "
        f"EX {result.metrics['ex']}; EX over valid references {result.metrics['ex_valid']}; "
        f"coverage {result.metrics['coverage']}"
    )
    if result.excepted:
        print(f"  excepted ({len(result.excepted)}): {', '.join(result.excepted)}")
    if result.stale_exceptions:
        stale = ", ".join(result.stale_exceptions)
        print(f"  stale exceptions (not reference_invalid this run): {stale}")
    for failure in result.failures:
        print(f"  FAILURE {failure}")


def _gate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.mode != "gold" or args.subset != "full" or args.resume is not None:
        parser.error("--gate gold judges whole suites: no --subset, --resume or --mode llm")
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="evaluate_v2_gate_") as scratch:
        out_root = args.out_root if args.out_root is not None else Path(scratch)
        for suite in args.suite:
            try:
                result = gold_gate(
                    args.suites_dir / f"{suite}.jsonl",
                    args.suites_dir / f"{suite}.gold_exceptions.txt",
                    out_root=out_root,
                    work_limit=args.work_limit,
                    max_rows=args.max_rows,
                )
            except (ResumeRefused, SuiteError, ValueError, OSError) as exc:
                print(f"{suite}: refused: {redact_dsn(str(exc))}", file=sys.stderr)
                return 2
            _print_gate(result)
            if not result.passed:
                exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.resume is not None and len(args.suite) != 1:
        parser.error("--resume continues one run, so it takes exactly one --suite")
    if args.gate is not None:
        return _gate(args, parser)
    if args.out_root is None:
        args.out_root = Path("evaluation/results")

    exit_code = 0
    for suite in args.suite:
        config = _config(args, suite)
        try:
            cases = select_cases(config)
            result = run_suite(cases, config=config, out_root=args.out_root, resume_dir=args.resume)
        except (ResumeRefused, SuiteError, ValueError, OSError) as exc:
            print(f"{suite}: refused: {redact_dsn(str(exc))}", file=sys.stderr)
            return 2
        manifest = result.manifest
        print(
            f"{suite}: {manifest.status}, citable={manifest.citable}, "
            f"{len(result.rows)} cases, {manifest.outage_count} outage(s)"
        )
        print(f"  {result.run_dir}")
        if manifest.status != "complete":
            print(f"  resume with: --resume {result.run_dir}")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
