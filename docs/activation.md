# Install, enable and roll back

`hermes-hindsight-coding` is an independent adapter, not the official native Hindsight plugin. It uses the project's Codex bank; it does not automatically read, import or delete `hermes-default`. The Hindsight service and database must already be running. A profile must select this exclusive provider explicitly.

## Install a reviewed release

1. Check that the profile has a positively scoped local terminal and already permits in-process plugins. The provider is inactive if plugin-host RPC does not attest the caller's terminal policy; do not loosen global isolation to get around this.
2. Set `REVIEWED_SHA` to the full verified commit from GitHub. Install with `hermes -p default plugins install https://github.com/pnpwin/hermes-hindsight-coding.git --ref "$REVIEWED_SHA" --no-enable --no-deps`. Treat source trust and any dependency prompt explicitly; this release does not need pip or npm at install time. Use `hermes -p default config path` to find the profile home. Confirm the installed commit, bridge hash and `hermes -p default plugins doctor hermes-hindsight-coding --ci` before enablement.
3. Follow [engine/README.md](../engine/README.md) to verify the pinned Node distribution. Store the verified binary as `plugin-data/hermes-hindsight-coding/runtime/node/bin/node` under the selected profile, and preserve its license. The checked release supports the pinned macOS Apple Silicon runtime.
4. Create `plugin-data/hermes-hindsight-coding/config.json` in the selected profile with mode 0600 and no symlink path components. Substitute the actual Codex Coding Agents config and only the physical workspaces you want to share. The canonical home path, if included literally, approves a non-Git session starting at home, not all its children. For example (substitute every placeholder):

```json
{
  "config_path": "/absolute/path/to/coding-agent.json",
  "allowed_roots": ["/absolute/path/to/project"],
  "allowed_api_url": "http://127.0.0.1:8888",
  "writes": true,
  "allow_bank_default_strategy": true,
  "create_missing_banks": false,
  "seed": false,
  "survey": false,
  "manage_bank_config": false
}
```

`writes:true` in this profile permits completed, sanitized turns from fresh primary local sessions to be retained for every bank this policy and Coding Agents route allow. It does not retroactively retain prior conversations. The package itself defaults to false when the field is omitted. The Hindsight service may send text for extraction or retrieval to the inference routes it has configured, even on a loopback API. Only enable writes in a profile whose retention and inference destinations have been approved. `allow_bank_default_strategy` permits the verified 1.2.1 behavior for banks without a `conversation` override; it does not change existing bank policy. Only enable `create_missing_banks` if the serving instance has no provisioning template, as explained in the README.

## Switch and verify

- Record the old provider, plugin enable state and config fingerprint. Drain relevant sessions and workers before changing providers. Inspect the installed Codex MCP `hindsight_diagnose` and the Hermes resolver for each intended cwd, without sending a project prompt. `hindsight_diagnose` proves routing, not that a bank exists. Verify the service and exact bank readbacks separately.
- With `writes:false` initially, enable the new plugin and select it: `hermes -p default plugins enable hermes-hindsight-coding` and `hermes -p default config set memory.provider hermes-hindsight-coding`. Verify from a fresh, actually scoped runtime. If replacing old native `hindsight`, disable it in that profile with `hermes -p default plugins disable hindsight` without uninstalling it. Use `writes:true` in the profile only once scope and inference are verified. Do not enable seed or survey merely to enable conversation retain.
- `scripts/verify_runtime.py --scope mock` tests real Hermes and installed Codex MCP against fixtures. `--scope existing-read --bank ID --confirm-bank ID` verifies both clients with exactly two bank-scoped page-tree GETs. It uses an explicit fixture mapping; separately test real cwd routing via the installed diagnosis. The `disposable` verifier requires a pre-provisioned, exclusively reserved, empty `ht-hermes-parity-<RUN_ID>` bank and the documented `--reservation` marker. Do not aim this mode at working banks. Verify retain processing and both clients' recall on a disposable bank before relying on the writer; fixture success alone does not attest live extraction.
- For a Desktop check, use fresh scoped sessions, attest the target gateway profile, physical cwd and returned session ID **before** any synthetic prompt, and compare installed Codex MCP diagnoses for both directories. Reopen one session and verify its bank remains pinned. Retaining unsupported inherited history remains fail-closed.

## Rollback

Stop accepting new turns in the affected profile, gracefully release its sessions and drain the memory worker and submitted operations. Verify strict liveness; do not restart unrelated gateways. Run `hermes -p default memory off` and `hermes -p default plugins disable hermes-hindsight-coding`. To return to the old configured provider, restore only the captured selector and enable state. In a fresh runtime check which tools/selector are available and confirm that no retain reaches the adapter. Do not delete Hindsight banks, data or installed plugins during rollback.
