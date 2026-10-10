"""Run an evaluation suite under the v2 contract and write one result directory per suite.

    uv run python scripts/evaluate_v2.py --suite demo --mode gold
    uv run python scripts/evaluate_v2.py --suite spider_dev --subset subset200 \\
        --mode llm --provider ollama --model llama3:latest
    uv run python scripts/evaluate_v2.py --suite bird_dev --mode llm ... --resume DIR
    uv run python scripts/evaluate_v2.py --gate gold --suite demo safety
    uv run python scripts/evaluate_v2.py --gate regression --new DIR --baseline DIR

`--gate gold` runs each suite in gold mode into a temporary directory (or `--out-root`) and
judges it by spec §4.4: valid references self-match, every `reference_invalid` ID is on
`<suites-dir>/<suite>.gold_exceptions.txt`, non-answerable cases are well-formed.

`--gate regression` compares a new complete, citable run with a baseline run of the same
suite and settings (spec §4.5) and fails on a real EX drop: a paired 95 % interval entirely
below zero, or a point drop of 5 points or more. Exit 0 pass, 1 fail, 2 refused (incompatible,
incomplete, uncitable, case IDs differ).

Each run writes `<out-root>/<run id>/{manifest.json,cases.csv,report.md}`; nothing is ever
overwritten. `--resume DIR` continues an incomplete run in place and is refused - exit code
2, naming the fields - unless the same arguments reproduce the saved identity.

`--ollama-think` sets Ollama's think flag for `--mode llm --provider ollama` and is a usage
error anywhere else. It defaults to `off`: the 512-token output cap is the SQL answer's
budget, and thinking spent inside it can leave no answer. The value is part of the run
identity, so `-think-off` or `-think-on` appears in the directory name.

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
from text_to_sql_agent.evaluation_v2.gates import (  # noqa: E402
    FLOOR_POINTS,
    GateResult,
    PairedChange,
    RegressionRefused,
    format_ids,
    gold_gate,
    regression_gate,
)
from text_to_sql_agent.evaluation_v2.runner import (  # noqa: E402
    ResumeRefused,
    RunConfig,
    run_suite,
    select_cases,
)

DEFAULT_SUITES_DIR = Path("evaluation/suites")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--suite", nargs="+", help="suite name(s), e.g. demo")
    parser.add_argument(
        "--subset", default="full", help="'full', or a subset name such as subset200"
    )
    parser.add_argument("--mode", choices=["gold", "llm"], default=None, help="default: gold")
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
    parser.add_argument(
        "--ollama-think",
        choices=["off", "on", "default"],
        default=None,
        help="Ollama's think flag, for --mode llm --provider ollama only: off sends think: "
        "false, on sends think: true, default sends no flag and leaves the model's own "
        "setting. Unset means off: thinking would spend the 512-token output cap meant for "
        "the SQL",
    )
    parser.add_argument("--resume", type=Path, metavar="DIR", default=None)
    parser.add_argument(
        "--gate",
        choices=["gold", "regression"],
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
    parser.add_argument("--new", type=Path, metavar="DIR", help="--gate regression: the new run")
    parser.add_argument(
        "--baseline", type=Path, metavar="DIR", help="--gate regression: the baseline run"
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
        ollama_think=args.ollama_think,
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
        print(f"  excepted ({len(result.excepted)}): {format_ids(result.excepted)}")
    if result.stale_exceptions:
        stale = format_ids(result.stale_exceptions)
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


def _change_line(label: str, change: PairedChange) -> str:
    i = change.interval
    return (
        f"  {label}: {change.old_correct}/{i.n} -> {change.new_correct}/{i.n}, "
        f"change {i.point * 100:+.1f} points, 95% paired interval "
        f"[{i.low * 100:+.1f}, {i.high * 100:+.1f}] points"
    )


def _print_regression(result: GateResult) -> None:
    detail = result.regression
    assert detail is not None
    print(f"{result.suite}: regression gate {'PASS' if result.passed else 'FAIL'}")
    print(
        f"  new {detail.new_commit[:8]} vs baseline {detail.old_commit[:8]}"
        f"{' (prompt changed)' if detail.prompt_changed else ''}"
    )
    print(_change_line("EX (answerable cases; the verdict rests on this)", detail.ex))
    print(
        f"  point drop {detail.drop_points + 0.0:.1f} points (floor {FLOOR_POINTS}; negative is an "
        "improvement); "
        f"interval below zero: {detail.interval_below_zero}; "
        f"floor breached: {detail.floor_breached}"
    )
    if detail.safety is not None:
        print(
            _change_line("safety accuracy (reported only, not part of the verdict)", detail.safety)
        )
    for label, failed in (
        ("new run", detail.generation_failures_new),
        ("baseline", detail.generation_failures_old),
    ):
        if failed:
            print(
                f"  {label}: {len(failed)} generation failure(s), scored as not correct as in "
                f"headline EX: {format_ids(failed)}"
            )
    for failure in result.failures:
        print(f"  FAILURE {failure}")


def _regression(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.new is None or args.baseline is None:
        parser.error("--gate regression needs --new DIR and --baseline DIR")
    try:
        result = regression_gate(args.new, args.baseline)
    except RegressionRefused as exc:
        print(f"regression gate refused: {redact_dsn(str(exc))}", file=sys.stderr)
        for name, new_value, old_value in exc.differences:
            print(
                f"  {name}: new {redact_dsn(new_value)!r} vs baseline {redact_dsn(old_value)!r}",
                file=sys.stderr,
            )
        return 2
    _print_regression(result)
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Before `--mode` defaults to gold: a flag that is never sent must not reach an identity.
    if args.ollama_think is not None and (args.mode != "llm" or args.provider != "ollama"):
        parser.error("--ollama-think applies only to --mode llm --provider ollama")
    if args.gate == "regression":
        if args.suite or args.mode is not None:
            parser.error("--gate regression compares two run directories: no --suite or --mode")
        return _regression(args, parser)
    if args.new is not None or args.baseline is not None:
        parser.error("--new and --baseline apply only to --gate regression")
    if args.mode is None:
        args.mode = "gold"
    if not args.suite:
        parser.error("the following arguments are required: --suite")
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
