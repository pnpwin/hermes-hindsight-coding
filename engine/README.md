# Pinned Coding Agents source

`UPSTREAM.json` binds npm release 0.8.7 to the immutable commit and archive integrity. The npm gitHead and the dereferenced upstream release tag must agree. The four shipped Codex hooks/MCP entry files were compared byte-for-byte with the npm archive.

From the Hermes scratch root use a sparse, filtered Git fetch of the pinned upstream tag (a whole shallow clone timed out):

```sh
U="$HOME/.hermes/cache/scratch/hindsight-coding-087-sparse"
git init "$U"
git -C "$U" remote add origin https://github.com/vectorize-io/hindsight.git
git -C "$U" sparse-checkout set hindsight-integrations/coding-agents
GIT_TERMINAL_PROMPT=0 git -C "$U" -c http.version=HTTP/1.1 fetch --depth 1 --filter=blob:none origin refs/tags/integrations/coding-agents/v0.8.7
git -C "$U" checkout --detach FETCH_HEAD
test "$(git -C "$U" rev-parse HEAD)" = 329f8a5ac9baa5567f4516a24dc9fd0f753a75ae
cd "$U/hindsight-integrations/coding-agents"
env -u NODE_EXTRA_CA_CERTS npm ci --ignore-scripts --no-audit --no-fund
env -u NODE_EXTRA_CA_CERTS npm run build
```

The full upstream suite needs its documentation fixtures. Add them to the sparse checkout before running tests:

```sh
git -C "$U" -c http.version=HTTP/1.1 sparse-checkout add hindsight-docs/src/lib hindsight-docs/static/img/harness
N="$HOME/.codex/runtimes/codex-lcm/node-22.22.3/bin"
env -u NODE_EXTRA_CA_CERTS PATH="$N:$PATH" node --version
env -u NODE_EXTRA_CA_CERTS PATH="$N:$PATH" node node_modules/vitest/vitest.mjs run --exclude '**/*.e2e.test.ts' --maxWorkers=1 --reporter=dot
```

Baseline on this Mac: Node 22.22.3, 83 files and 1,410 tests passed. Host Node 26.13.1 failed the reflect deadline test (`UND_ERR_INVALID_ARG` from the Undici dispatcher) and the survey supervisor test. Node 22 from the Codex runtime is used as a read-only baseline executable, not as a production dependency of this plugin; select and pin a dedicated Node 22 before activation. `NODE_EXTRA_CA_CERTS` pointed to a missing file; unset only for each baseline command, not for the user's profile. Do not run the upstream installer or change the installed Codex bundles.

For bridge development, the checked official macOS ARM64 binary is separate from Codex. Its archive and extracted binary hashes are in `NODE.json`. In a fresh scratch path:

```sh
D="$HOME/.hermes/cache/scratch/coding-node-22.22.3"
mkdir -m 700 "$D"
A="$D/node-v22.22.3-darwin-arm64.tar.gz"
curl -fsSL https://nodejs.org/dist/v22.22.3/node-v22.22.3-darwin-arm64.tar.gz -o "$A"
printf '%s  %s\n' 0da7ff74ef8611328c8212f17943368713a2ad953fb7d89a8c8a0eae87c23207 "$A" | shasum -a 256 -c -
tar -xzf "$A" -C "$D" --strip-components 1 node-v22.22.3-darwin-arm64/bin/node
```

For an offline release build, use a clean scratch checkout of the pin and `npm ci --offline --ignore-scripts --no-audit --no-fund` from a pre-populated npm cache. From this adapter repo:

```sh
mkdir -p bridge/dist
GIT_NO_LAZY_FETCH=1 /usr/bin/python3 scripts/build-bridge.py --upstream "$U" \
  --out "$PWD/bridge/dist/hermes-bridge.mjs" --notice "$PWD/engine/NOTICE.md"
```

The builder checks both pins and the exact upstream diff before bundling with the lockfile's esbuild. The `module,main` resolution order selects the ESM jsonc-parser entry; the default UMD entry failed on dynamic `require` even though `node --check` passed. It writes the bundle, `.sha256`, `.schemas.json`, and the optional license inventory from actual bundled modules; it rejects non-Node external imports. The installable checkout contains the three bridge files and `NOTICE.md`; it does not need npm or download dependencies during installation. The separately verified Node executable is required per profile. Verified bundle bytes are sent in memory to the isolated worker, which never loads a mutable bundle pathname. `resolve` performs no HTTP. The client rejects redirects including 307/308. Patch 0006 records explicit adapter-only exceptions to the structural upstream tests: Hermes must not self-update, launch a daemon, or bypass its guarded HTTP transport.

Filesystem scope failures intentionally depart from Codex parity. Write `mapPathToBank`, `paths`, and `optInPaths` using physical paths, not aliases; the physical cwd and checkout determine both authorisation and naming. Ordinary checkouts, linked worktrees with a reciprocal Git backlink, standalone bare repositories, and conventional hidden bare hubs are supported. Malformed/dangling `.git`, unverified indirections (including submodule-style pointers without a worktree backlink), cross-repository fallback, and unreadable/oversized Git metadata disable the route. A bound request records directory device/inode identifiers and uses one inspected Git snapshot; no Git pointer content is read outside approved roots. These checks constrain repository data, not a hostile process with the same OS account and write access to the Hermes installation itself.
