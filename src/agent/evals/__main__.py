"""Run the offline evaluation suite from the command line."""

import argparse
import asyncio
import sys
from pathlib import Path

from agent.evals.dataset import load_evaluation_manifest
from agent.evals.reporting import report_json, report_markdown
from agent.evals.runner import run_evaluation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, help="Optional manifest path")
    parser.add_argument("--json-output", type=Path, help="Write canonical JSON evidence")
    parser.add_argument("--markdown-output", type=Path, help="Write GitHub-facing Markdown")
    parser.add_argument(
        "--verify-json",
        type=Path,
        help="Fail when a committed JSON report differs from a fresh run",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress Markdown stdout")
    return parser


async def _run(args: argparse.Namespace) -> int:
    manifest = load_evaluation_manifest(args.dataset)
    report = await run_evaluation(manifest)
    json_text = report_json(report)
    markdown_text = report_markdown(report)

    for path, content in (
        (args.json_output, json_text),
        (args.markdown_output, markdown_text),
    ):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    verification_ok = True
    if args.verify_json is not None:
        try:
            verification_ok = args.verify_json.read_text(encoding="utf-8") == json_text
        except OSError:
            verification_ok = False
        if not verification_ok:
            sys.stderr.write("Committed evaluation report is missing or stale.\n")

    if not args.quiet:
        sys.stdout.write(markdown_text)
    return 0 if report.ready and verification_ok else 1


def main() -> int:
    """Parse arguments and return a CI-compatible process status."""
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
