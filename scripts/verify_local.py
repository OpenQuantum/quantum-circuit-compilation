"""Run unchanged official full verifiers and save stdout/stderr and timing."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('task', choices=['invsqrt', 'dqi', 'dfthcsos'])
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=7200)
    args = parser.parse_args()
    modules = {'invsqrt': 'invsqrt.verify_submission_invsqrt',
               'dqi': 'dqi.verify_submission_dqi', 'dfthcsos': 'dfthcsos.verify'}
    path = ROOT / 'official/data/sample_submission' / (args.task if args.task == 'dfthcsos' else args.task + '.kmb')
    command = [sys.executable, '-m', modules[args.task], str(path)]
    if args.task != 'dfthcsos':
        command += ['--max-workers', str(args.workers)]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT / 'official/data/verifiers')
    env['OPENBLAS_NUM_THREADS'] = env['VECLIB_MAXIMUM_THREADS'] = '1'
    start = time.monotonic()
    prefix = ROOT / 'runs' / (args.task + '-full')
    with prefix.with_suffix('.json').open('w') as stdout, prefix.with_suffix('.stderr').open('w') as stderr:
        try:
            completed = subprocess.run(command, cwd=ROOT, env=env, stdout=stdout, stderr=stderr, timeout=args.timeout)
            result = {'exit_code': completed.returncode, 'status': 'finished'}
        except subprocess.TimeoutExpired:
            result = {'exit_code': None, 'status': 'timeout'}
    result.update(task=args.task, wall_seconds=time.monotonic() - start,
                  scope='full official defaults, local unsalted tests; not Kaggle secret salts', command=command)
    (ROOT / 'runs' / (args.task + '-execution.json')).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['exit_code'] == 0 else 1)


if __name__ == '__main__':
    main()
