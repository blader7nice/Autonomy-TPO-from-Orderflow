"""flowlevels command-line entry point."""

from __future__ import annotations

import argparse
import sys

from flowlevels import __version__
from flowlevels.config import config_hash, git_commit, load_config
from flowlevels.settings import load_settings


def _cmd_info(_: argparse.Namespace) -> int:
    settings = load_settings()
    cfg = load_config(settings.config_path)
    print(f"flowlevels {__version__}")
    print(f"commit      {git_commit()}")
    print(f"config      {settings.config_path} (v{cfg.version}, hash {config_hash(cfg)})")
    print(f"data_dir    {settings.data_dir}")
    print(f"symbols     {', '.join(cfg.symbols)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowlevels")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_info = sub.add_parser("info", help="Print version, commit and config hash")
    p_info.set_defaults(func=_cmd_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
