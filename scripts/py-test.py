import argparse
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--python', type=Path, help='Explicit test venv Python; bypass Hermes bootstrap')
parser.add_argument('pattern')
opts = parser.parse_args()
args = ['discover', '-s', 'tests', '-p', opts.pattern, '-v']
if opts.python is not None:
    if not opts.python.is_absolute() or not opts.python.is_file():
        parser.error('--python must be an absolute path to an existing venv interpreter')
    argv = [str(opts.python), '-I', '-B', '-m', 'unittest', *args]
else:
    argv = json.loads(subprocess.check_output([
        'hermes', '--print-runtime-command', '--module', 'unittest', '--', *args,
    ], text=True))
raise SystemExit(subprocess.run(argv, check=False).returncode)
