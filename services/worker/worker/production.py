"""Lean scheduled-worker entry point; load only the requested production lane."""
import argparse
import json

from spe.publishing.pipeline import production_cycle


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--lane', choices=('odds', 'inputs', 'settlement', 'touchdowns'), required=True)
    args = parser.parse_args()
    print(json.dumps(production_cycle(args.lane), default=str), flush=True)
