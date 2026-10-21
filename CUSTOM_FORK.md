# Zach's Hermes branch

`stable/hermes` on <https://github.com/zachristmas/hermes-agent> is the shared
Mac/Windows source branch. Official updates come from
<https://github.com/NousResearch/hermes-agent>, branch `main`.

The initial consolidation uses official base
`3ab0930318ad970b4ec102838f75f62973e188ec` and reviewed local semantic changes
through `10dc600bb2ee061015f34f6e826f4b9eb121ce70`. It carries interactive
Herdr dispatch, board/model routing, reports and executor links, durable
Kanban settlement/acceptance/recovery, task refs, comments and attachment
previews, plus the required worker persistence, route identity and plugin
injection repairs. Discord Realtime/streaming voice and the separate legacy
Bot Chat customization are excluded; ordinary upstream capabilities remain.
Original source refs/worktrees and the detailed source/test receipts are
preserved in the operator's `hermes-stable-20261021-evidence` directory.

## Upstream merges

Use an independent maintenance checkout. Preserve dirty work and stop if the
expected branch or remotes differ. Do not merge feature branches wholesale.

```sh
git remote -v
git fetch https://github.com/zachristmas/hermes-agent.git stable/hermes
git worktree add -b integration/upstream-YYYYMMDD /path/to/maintenance FETCH_HEAD
cd /path/to/maintenance
git fetch https://github.com/NousResearch/hermes-agent.git main
git merge --no-ff FETCH_HEAD
```

Resolve conflicts by behavior, retaining newer upstream ownership/profile and
runtime guards. Review `git diff` and the merge result. Use a PM test environment
and the supported runner; never test against an installed profile's state:

```sh
scripts/run_tests.sh tests/hermes_cli/test_kanban_direct_herdr*.py tests/hermes_cli/test_kanban_settlement*.py tests/hermes_cli/test_kanban_manual_executor.py tests/hermes_cli/test_kanban_ref_cli.py tests/tools/test_kanban_unknown_arguments.py tests/gateway/test_kanban_maintenance_when_paused.py tests/gateway/test_automation_route_identity.py tests/gateway/test_plugin_injection_outcome.py tests/agent/test_worker_rpc_busy_owner.py tests/hermes_cli/test_update_parked_branch_guard.py
python scripts/check
```

For desktop changes, also run the selected Kanban drawer/media UI tests and
`npm run typecheck` in `apps/desktop`. Run native Windows contracts before
aligning Windows. Keep normal commit signing and hooks.

Fetch immediately before publication and require the published branch to be
an ancestor of the tested merge. A concurrent update requires reconciliation
and affected checks; never force-push:

```sh
git fetch https://github.com/zachristmas/hermes-agent.git stable/hermes
git merge-base --is-ancestor FETCH_HEAD HEAD
git push git@github.com:zachristmas/hermes-agent.git HEAD:refs/heads/stable/hermes
git ls-remote https://github.com/zachristmas/hermes-agent.git refs/heads/stable/hermes
```

Start new work on a feature branch from the published `stable/hermes` and
merge reviewed/tested work back through the same publication step.

Publication uses the Mac's existing personal SSH identity (`zachristmas`).
HTTPS on that host can select the corporate account instead; do not change
global credentials to work around that. HTTPS fetches and ref readback remain
valid. Keep push destinations on Zach's fork and verify the authenticated
account before publishing from another host.

## Installed checkout alignment and updates

On each host independently, require a clean checkout, record its HEAD and
create a uniquely named recovery ref before changing the installed branch.
Keep `origin` pointed at Zach's fork and `upstream` at the official repository.
Fetch the fork and fast-forward `stable/hermes` to the exact published SHA;
never discard local commits or use `reset --hard` on an installed checkout.
If the branch is already checked out in a maintenance worktree, release that
worktree's branch by detaching it first, preserving its commit and files.

Keep `updates.parked_branch_strategy: update_in_place` in each host's effective
configuration. The updater behavior has a disposable-checkout contract in
`tests/hermes_cli/test_update_parked_branch_guard.py`. Use the explicit branch
when updating this custom source install:

```sh
hermes update --branch stable/hermes
```

`stable/hermes` is a Git branch, not the official `stable` release channel.
Do not select the official stable channel as a substitute for this branch.
Check the per-install update record and effective parked-branch strategy on
both hosts; never copy configuration, databases, sessions or credentials
between hosts.

## Gateway rollout and recovery

Installed HEAD and running code are separate facts. Before a restart, verify
the exact live PID/start fingerprint, code SHA, owning supervisor and launch
command, current activity, and an independent safe control shell. Preserve
the existing supervisor; do not start a competing foreground gateway. Use
the supported supervised graceful restart only with authorization and safe
draining. If activity or supervisor verification prevents a restart, leave
the process alone and report installed versus live revisions separately.

After restart, require a fresh PID/start fingerprint, the published code SHA,
healthy runtime heartbeat and expected connected adapters/profiles on each
host. A successful command or branch name alone is insufficient.

For recovery, stop further rollout, preserve diagnostic evidence, and select
the recorded recovery commit in a clean checkout without deleting refs or
worktrees. Refresh its PM environment only if required by that revision,
then use the same authorized supervisor/drain procedure. Never roll a live
gateway back by overwriting its databases or credentials.
