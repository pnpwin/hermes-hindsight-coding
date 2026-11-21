"""Verify the packaged provider. Live modes require explicit authorization for one bank."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path.home().resolve() / '.hermes/cache/scratch'
DISPOSABLE = r'ht-hermes-parity-[a-z0-9][a-z0-9-]{7,47}'
EXISTING = r'[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}'


def run_live(scope, bank, reservation=None):
    worker = ROOT / 'scripts/live_runtime.py'
    with tempfile.TemporaryDirectory(prefix='hindsight-live-home-', dir=SCRATCH) as home:
        env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'LC_ALL') if k in os.environ}
        env.update(HERMES_HOME=home, TMPDIR=home, HERMES_SKIP_UPDATE_CHECK='1', GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL='/dev/null')
        argv = json.loads(subprocess.check_output([
            'hermes', '--print-runtime-command', '--module', 'unittest', '--', scope, bank, bank,
            *([reservation] if reservation else []),
        ], cwd=ROOT, env=env, text=True, timeout=20))
        anchor = "runpy.run_module('unittest', run_name='__main__', alter_sys=True)"
        positions = [i for i, arg in enumerate(argv) if isinstance(arg, str) and arg.count(anchor) == 1]
        if len(positions) != 1 or positions[0] < 1 or argv[positions[0] - 1] != '-c':
            raise SystemExit('BLOCKED: unrecognized Hermes runtime launcher')
        index, = positions
        argv[index] = argv[index].replace(anchor, f"runpy.run_path({str(worker)!r}, run_name='__main__')")
        result = subprocess.run(argv, cwd=ROOT, env=env, timeout=330)
        if result.returncode:
            raise SystemExit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', required=True, choices=('mock', 'existing-read', 'disposable'))
    parser.add_argument('--bank')
    parser.add_argument('--confirm-bank', help='Repeat the exact separately authorized live bank ID')
    parser.add_argument('--reservation', help='Operator-installed reservation marker for disposable only')
    opts = parser.parse_args()
    if opts.scope == 'disposable' and not re.fullmatch(DISPOSABLE, opts.bank or ''):
        parser.error('disposable scope requires --bank ht-hermes-parity-<RUN_ID>')
    if opts.scope != 'mock':
        if not re.fullmatch(EXISTING, opts.bank or ''):
            parser.error('live scope requires a valid literal --bank')
        if opts.confirm_bank != opts.bank:
            parser.error('--confirm-bank must exactly match --bank')
        if opts.scope == 'disposable':
            if not re.fullmatch(r'[0-9a-f]{32}', opts.reservation or ''):
                parser.error('disposable requires --reservation from a separately provisioned exclusive bank')
            run_live(opts.scope, opts.bank, opts.reservation)
        else:
            if opts.reservation is not None:
                parser.error('--reservation only applies to disposable')
            run_live(opts.scope, opts.bank)
        return
    if opts.bank is not None or opts.confirm_bank is not None or opts.reservation is not None:
        parser.error('mock scope takes no external bank')
    with tempfile.TemporaryDirectory(prefix='hindsight-runtime-', dir=SCRATCH) as name:
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/py-test.py'), 'runtime_cases.py'], cwd=ROOT,
                                env=os.environ | {'HERMES_HOME': name}, capture_output=True, text=True, timeout=90)
    report = result.stdout + result.stderr
    print(report, end='')
    if result.returncode or not re.search(r'(?m)^Ran 4 tests in ', report) or not re.search(r'(?m)^OK$', report):
        raise SystemExit(result.returncode or 1)
    print('PASS scope=mock bank=coding-agent::project provider=hermes-hindsight-coding')


if __name__ == '__main__':
    main()
