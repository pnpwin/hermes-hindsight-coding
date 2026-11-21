"""Build the Hermes bridge in a dedicated sparse scratch checkout. No network."""

import argparse
import hashlib
import json
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'engine'
SCRATCH = Path.home().resolve() / '.hermes/cache/scratch'


def diff_sections(text):
    return sorted(part for part in re.split(r'(?m)(?=^diff --git )', text) if part)


def bundle_notice(pkg, meta, upstream_license, pin):
    packages = {}
    modules = pkg / 'node_modules'
    for output in json.loads(meta.read_text())['outputs'].values():
        for source, info in output['inputs'].items():
            if not info['bytesInOutput'] or not source.startswith('node_modules/'):
                continue
            source_path = (pkg / source).resolve(strict=True)
            for directory in source_path.parents:
                if directory == modules or modules not in directory.parents:
                    raise SystemExit(f'bundled dependency is not under node_modules: {source}')
                manifest = directory / 'package.json'
                if manifest.is_file():
                    manifest_data = json.loads(manifest.read_text())
                    if manifest_data.get('name') and manifest_data.get('version'):
                        packages[(manifest_data['name'], manifest_data['version'])] = directory
                        break
    parts = ['# Third-party notices', '',
             f'The bridge includes Vectorize Hindsight Coding Agents {pin["version"]} ({pin["gitCommit"]}) '
             'with the Hermes adapter patches in this repository.', '', '## Upstream license', '', upstream_license.rstrip(), '',
             '## Bundled dependency licenses', '']
    for (name, version), directory in sorted(packages.items()):
        files = sorted(p for p in directory.iterdir() if p.is_file() and
                       re.fullmatch(r'(?:licen[sc]e|copying|notice|copyright)(?:[-.].*)?', p.name, re.I))
        if not files:
            raise SystemExit(f'missing license files for {name}@{version}')
        parts.extend((f'### {name}@{version}', ''))
        for path in files:
            parts.extend((f'**{path.name}**', '', path.read_text(encoding='utf-8').rstrip(), ''))
    return '\n'.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', type=Path, default=SCRATCH / 'hindsight-coding-087-sparse')
    parser.add_argument('--node', type=Path, default=SCRATCH / 'coding-node-22.22.3/bin/node')
    parser.add_argument('--out', type=Path, default=SCRATCH / 'hermes-hindsight-coding-bridge-087.mjs')
    parser.add_argument('--notice', type=Path, help='write license inventory from actual bundle inputs')
    options = parser.parse_args()
    upstream = options.upstream.resolve(strict=True)
    pin = json.loads((ENGINE / 'UPSTREAM.json').read_text())
    runtime = json.loads((ENGINE / 'NODE.json').read_text())
    node = options.node.resolve(strict=True)
    with node.open('rb') as binary:
        if hashlib.sha256(binary.read()).hexdigest() != runtime['nodeSHA256']:
            raise SystemExit('Node artifact does not match engine/NODE.json')
    if upstream == SCRATCH or SCRATCH not in upstream.parents:
        raise SystemExit('upstream must be a dedicated scratch checkout')

    def git(*args):
        return subprocess.run(['/usr/bin/git', '-C', str(upstream), *args],
                              check=True, text=True, stdout=subprocess.PIPE).stdout

    if git('rev-parse', 'HEAD').strip() != pin['gitCommit'] or git('diff', '--cached', '--name-only').strip():
        raise SystemExit('upstream commit or staging area does not match the pin')
    pkg = upstream / pin['directory']
    if json.loads((pkg / 'package.json').read_text())['version'] != pin['version']:
        raise SystemExit('upstream package version does not match the pin')
    patches = sorted((ENGINE / 'patches').glob('*.patch'))
    expected = diff_sections(''.join(path.read_text() for path in patches))
    actual = diff_sections(git('--no-pager', 'diff', '--no-ext-diff', '--binary'))
    if any(section not in expected for section in actual):
        raise SystemExit('unexpected upstream source modifications; refusing to build')
    for patch in patches:
        patch_sections = diff_sections(patch.read_text())
        present = [section in actual for section in patch_sections]
        if all(present):
            continue
        if any(present):
            raise SystemExit(f'partially applied patch {patch.name}')
        git('apply', '--check', str(patch))
        git('apply', str(patch))
    if diff_sections(git('--no-pager', 'diff', '--no-ext-diff', '--binary')) != expected:
        raise SystemExit('applied patch fingerprint differs')
    for source in sorted((ENGINE / 'overlay/src').glob('hermes-*.ts')):
        shutil.copyfile(source, pkg / 'src' / source.name)

    output = options.out.absolute()
    if output.suffix != '.mjs' or not output.parent.is_dir():
        raise SystemExit('output must be an .mjs in an existing directory')
    env = {'PATH': str(node.parent) + ':/usr/bin:/bin', 'HOME': str(SCRATCH), 'TMPDIR': str(SCRATCH)}
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.bridge-build-') as temp:
        artifact = Path(temp) / 'hermes-bridge.mjs'
        meta = Path(temp) / 'bundle.metafile.json'
        subprocess.run([str(pkg / 'node_modules/.bin/esbuild'), 'src/hermes-bridge.ts', '--bundle',
                        '--platform=node', '--main-fields=module,main', '--target=node22', '--format=esm',
                        '--legal-comments=eof', f'--metafile={meta}', f'--outfile={artifact}'], cwd=pkg, env=env, check=True)
        subprocess.run([str(node), '--check', str(artifact)], env=env, check=True)
        externals = {i['path'] for entry in json.loads(meta.read_text())['outputs'].values()
                     for i in entry['imports'] if i.get('external')}
        if any(not name.startswith('node:') for name in externals):
            raise SystemExit(f'non-Node runtime dependencies: {sorted(externals)}')
        schema_json = subprocess.check_output([str(node), str(artifact), '--schemas'], env=env, timeout=15)
        if options.notice:
            options.notice.write_text(bundle_notice(pkg, meta, git('show', 'HEAD:LICENSE'), pin), encoding='utf-8')
        schemas = Path(temp) / 'tools.schemas.json'
        schemas.write_text(json.dumps(json.loads(schema_json), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        manifest = Path(temp) / 'bridge.sha256'
        manifest.write_text(digest + '\n', encoding='ascii')
        os.replace(manifest, output.with_suffix('.sha256'))
        os.replace(schemas, output.with_suffix('.schemas.json'))
        os.replace(artifact, output)
    print(f'PASS bundle={output} upstream={pin["gitCommit"]} node={runtime["version"]}')


if __name__ == '__main__':
    main()
