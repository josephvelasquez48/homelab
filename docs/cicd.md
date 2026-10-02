# CI/CD

Roadmap step 11. GitHub Actions tests and builds every service; Argo CD
deploys ([argocd.md](argocd.md)). CI never touches the cluster - it only
commits new image tags to git.

```
push -> test -> build arm64 image -> push to ghcr.io (:<12-char SHA> and :latest)
     -> commit the new tag to kubernetes/ [skip ci] -> Argo CD applies it
```

## Workflows

| Workflow | Builds | Deploys by editing |
|---|---|---|
| `ci.yml` | `homelab-api` (the FastAPI backend and worker) | `kubernetes/backend/{api,worker}.yaml` |
| `ci-dashboard.yml` | `homelab-dashboard` | `kubernetes/dashboard/dashboard.yaml` |
| `ci-chat.yml` | `homelab-chat` | `kubernetes/chat/` |
| `ci-zimsearch.yml` | `homelab-zimsearch` | `kubernetes/kiwix/` |
| `ci-adguard-exporter.yml` | `homelab-adguard-exporter` | `kubernetes/monitoring/` |
| `ci-phone.yml` | tests only - the phone bridge is a host service ([phone.md](phone.md)) | - |
| `ci-backup.yml` | tests only - the backup scripts run on the Mac and Pi | - |
| `pr-gate.yml` | nothing - the one check `main` requires (below) | - |

All run on GitHub-hosted runners. Images are `linux/arm64` only, since
both cluster nodes are arm64 ([node-migration.md](node-migration.md)).

**Database migrations** run as an Argo CD `PreSync` hook (`api-migrate`)
on every sync, before the new pods start.

## Merging: the PR gate

`main` has a ruleset (`terraform/github/ruleset.tf`) requiring one check,
`gate`, so PR auto-merge waits for CI. It can't require the apps' `test`
jobs: every workflow above is path-filtered, so a docs, Ansible or
Terraform PR runs none of them and would wait forever. `pr-gate.yml` runs
on every PR instead, waits for whichever workflows that commit triggered,
and fails if any failed - seconds for a docs PR, the full test run for an
app PR.

- **The deploy jobs bypass it with a deploy key.** Their `[skip ci]` tag
  bumps push straight to `main`, so their checkout uses the `ci-deploy`
  deploy key (write access, private half in the `DEPLOY_KEY` secret)
  instead of `GITHUB_TOKEN`, and the ruleset exempts deploy keys. Not the
  GitHub Actions app: on a personal repo a ruleset can only exempt roles
  and deploy keys, and the apply 422s. Nothing else is exempt, so direct
  pushes to `main` from your own account are rejected - go through a PR.
- **Don't put `[skip ci]` in a PR's commit message** - not even quoted, as
  in "the deploy jobs' [skip ci] commits". GitHub reads it anywhere in the
  head commit's message and runs no workflows, the gate included, so the
  PR can never merge.
- **Re-running a failed workflow doesn't re-run the gate.** Re-run the gate
  too once it's green, or push a commit.

## Problems found and fixed

| Problem | Cause | Fix |
|---|---|---|
| `permission_denied: write_package` on the first push | The ghcr.io package was first pushed by hand, so it wasn't linked to this repo | Gave the repo write access in the package settings |
| `kubectl` failed on the self-hosted runner | Its service doesn't read the user's `.zshenv`, so `KUBECONFIG` was unset | Set explicitly in the job (the runner is no longer used for deploys) |
| One of two deploy jobs failed with `rejected (fetch first)` | Two workflows triggered by one push both committed to `main`; the second push was behind | A shared `concurrency: git-deploy-main` group, plus `git pull --rebase` before pushing. Both are needed: concurrency orders the jobs, the rebase refreshes a checkout pinned to an older commit |

**`gh run rerun --failed` doesn't fix the race:** a rerun checks out the
original commit again, so it fails the same way.

## The self-hosted runner

**Removed on 2026-09-30.** A runner on the Pi (installed by the
`github_runner` Ansible role) polled GitHub over outbound HTTPS, so nothing
was exposed inbound. It ran the old deploy job, which used `kubectl`
against the private cluster. Since Argo CD, deploys only write to git, and
no workflow used it - it stayed installed in case something needed LAN
access later.

It went because the repo is public: GitHub's approval gate only holds back
pull requests from first-time contributors, so a returning contributor's
PR could have added a workflow that ran on the Pi, inside the LAN and next
to the cluster. GitHub advises against self-hosted runners on public repos.
The one LAN-side job left - telling Argo CD that main moved - is done by
`apps/argocd-refresh`, which only reads git. Unregistered on GitHub
(`config.sh remove`), its service uninstalled; the role is kept, out of
`site.yml`, in `ansible/playbooks/github-runner.yml`.
