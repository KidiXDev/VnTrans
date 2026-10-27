import argparse
import sys

from . import pipeline
from .model import VnTransError
from .progress import Progress


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="vntrans", description="Extract and rebuild visual novel scripts")
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="export translation JSON")
    extract.add_argument("input")
    extract.add_argument("--output", required=True)
    extract.add_argument("--engine", default="kirikiri")
    extract.add_argument("--encoding")
    extract.add_argument("--language-index", type=int, default=0)
    for name in ("validate", "rebuild"):
        cmd = commands.add_parser(name)
        cmd.add_argument("input")
        cmd.add_argument("--translations", required=True)
        cmd.add_argument("--wrap-columns", type=int)
        cmd.add_argument("--output-encoding")
        if name == "rebuild":
            cmd.add_argument("--output", required=True)
    args = vars(parser.parse_args(argv))
    operation = args.pop("command")
    try:
        with Progress() as progress:
            report = getattr(pipeline, operation)(**args, progress=progress)
    except KeyboardInterrupt:
        print("vntrans: cancelled", file=sys.stderr)
        return 130
    except (VnTransError, OSError) as exc:
        print(f"vntrans: {exc}", file=sys.stderr)
        return 1
    print(f"{operation}: {report.files} file(s), {report.units} unit(s)")
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0
