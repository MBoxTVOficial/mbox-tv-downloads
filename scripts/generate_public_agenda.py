"""GENERAL entry point for public football agendas only; no auxiliary API adapters."""
import argparse
from datetime import datetime
from pathlib import Path

from . import generate_sports_today as generator
from .generate_official_agenda import generate_official_agenda
from .sports_agenda_diagnostics import emit_result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    now = datetime.now(generator.argentina_timezone())
    feed, report, changed = generate_official_agenda(
        generator.ROOT, args.output or generator.REAL_OUTPUT, now.date(), now)
    return emit_result(feed, report, changed, now.date(), public=True)


if __name__ == '__main__':
    raise SystemExit(main())
