"""Run the supplied pipeline, add-ons and checks with one command."""
import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', default='data')
    parser.add_argument('--out', default='out')
    parser.add_argument('--prev', default=None)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    for script in ('pipeline.py', 'addons_v3.py', 'check.py'):
        command = [sys.executable, str(root / script), '--data', args.data, '--out', args.out]
        if script == 'check.py' and args.prev is not None:
            command += ['--prev', args.prev]
        completed = subprocess.run(command)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
