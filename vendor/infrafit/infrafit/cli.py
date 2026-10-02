"""infrafit 명령줄 진입점."""

from __future__ import annotations

import argparse

from infrafit import __version__


def _cmd_analyze(args: argparse.Namespace) -> int:
    from pathlib import Path

    from infrafit.pipeline import analyze

    ctx = analyze(args.source, Path(args.out), until=args.until, run_id=args.run_id)
    print(ctx.out_dir)
    return 0


def _cmd_kb_lint(args: argparse.Namespace) -> int:
    from infrafit.kb_lint import lint

    issues = lint()
    for issue in issues:
        print(issue)
    print(f"{len(issues)}개 문제")
    return 1 if issues else 0


def _cmd_check_run(args: argparse.Namespace) -> int:
    from pathlib import Path

    from infrafit.consistency import check_run

    issues = check_run(Path(args.run_dir))
    for issue in issues:
        print(issue)
    print(f"{len(issues)}개 문제")
    return 1 if issues else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="infrafit")
    parser.add_argument("--version", action="version", version=f"infrafit {__version__}")
    sub = parser.add_subparsers(dest="command")

    analyze = sub.add_parser("analyze", help="저장소를 분석한다")
    analyze.add_argument("source", help="로컬 경로 또는 GitHub URL")
    analyze.add_argument("--out", default="out", help="실행 결과 디렉터리 루트")
    analyze.add_argument("--until", default="S1", help="이 단계까지 실행")
    analyze.add_argument("--run-id", default=None)
    analyze.set_defaults(func=_cmd_analyze)

    check = sub.add_parser("check-run", help="실행 결과의 스키마·일관성 검사")
    check.add_argument("run_dir")
    check.set_defaults(func=_cmd_check_run)

    kb_cmd = sub.add_parser("kb", help="지식 베이스 도구")
    kb_sub = kb_cmd.add_subparsers(dest="kb_command")
    kb_lint = kb_sub.add_parser("lint", help="knowledge/ 형식 검사")
    kb_lint.set_defaults(func=_cmd_kb_lint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    return args.func(args)
