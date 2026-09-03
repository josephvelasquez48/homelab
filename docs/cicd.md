# CI/CD

## Log

- 2026-09-03: Pipeline built and verified with real runs, not just written
  and assumed correct - two real failures hit and fixed:
  1. **`permission_denied: write_package`** on the first build-and-push
     run. The `ghcr.io/homelab-api` package had been pushed manually (with
     a personal token) back when it was first created for the Kubernetes
     migration, so it was never linked to this repo's Actions permissions -
     the default `GITHUB_TOKEN` had no write access to it. Fixed via the
     package's own settings (Manage Actions access -> add the `homelab`
     repo with `Write` role), not a workflow change.
  2. **Same `KUBECONFIG` default-path issue as the original Pi kubectl
     setup**, in a new context: the self-hosted runner's systemd service
     environment doesn't inherit the `joe` user's interactive-shell
     `.zshenv`, so `kubectl` fell back to `/etc/rancher/k3s/k3s.yaml`
     (root-only) and failed with a permission error. Fixed by setting
     `KUBECONFIG` explicitly in the deploy job rather than assuming the
     shell environment carries it.

  After both fixes, a real push ran clean end-to-end: `test` (11s) ->
  `build-and-push` (63s, multi-arch) -> `deploy` (40s) on the self-hosted
  runner, no manual `kubectl` intervention. Confirmed the running pods'
  image tag matched the exact commit SHA CI built (not just that the
  workflow reported success), and `api.home/health` stayed green through
  the rollout.

- 2026-09-03: **Superseded by Argo CD** (roadmap step 12, see
  [docs/argocd.md](argocd.md)) later the same day - `deploy` no longer
  runs `kubectl` against the cluster at all. It commits the new image tag
  into `kubernetes/backend/*.yaml` instead and lets Argo CD apply it, which
  also means it moved back to a GitHub-hosted runner (writing to git needs
  no private network access, unlike the `kubectl`-based version below).
  The self-hosted runner setup stays documented and installed - nothing
  currently in the pipeline needs it, but it's available if a future step
  does.

Roadmap step 11:

```
git push -> GitHub Actions -> Tests -> Build Docker image -> Container registry -> Deployment
```

## Pipeline (current)

`.github/workflows/ci.yml`, three jobs, all GitHub-hosted:

1. **test** - `uv sync` + `pytest` against `apps/api`.
2. **build-and-push** (only on push to `main`) - multi-arch
   (`linux/amd64,linux/arm64`) build via `docker buildx`, pushed to
   `ghcr.io/josephvelasquez48/homelab-api` tagged both `:latest` and
   `:<12-char commit SHA>`.
3. **deploy** - `sed`-replaces the image tag in
   `kubernetes/backend/{api,worker}.yaml` and commits (`[skip ci]`, to
   avoid the commit re-triggering this same workflow against itself).
   Argo CD picks up the change on its own polling cycle - see
   [docs/argocd.md](argocd.md) for that half of the pipeline.

## Why a self-hosted runner existed (historical - see the log above)

GitHub-hosted runners can't reach `192.168.1.253:6443` (the K3s API server)
- it's a private home network with no port forwarded, and it should stay
that way (exposing a Kubernetes API server to the public internet is
squarely the kind of thing to avoid). A self-hosted runner solves this the
standard way: it's a process on the Pi that polls GitHub over outbound
HTTPS for work, so nothing needs to be exposed inbound at all.

Only the **deploy** job runs there - test and build stay on GitHub-hosted
runners deliberately. Building the amd64 half of a multi-arch image via
QEMU emulation on arm64 Pi hardware would be slow (emulation overhead on
top of an already-modest CPU); GitHub's runners build it natively. The
self-hosted runner only needs `kubectl` and network access to the cluster,
not a full Docker buildx toolchain.

### Setup

```bash
# On the Pi
mkdir ~/actions-runner && cd ~/actions-runner
curl -o runner.tar.gz -L https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-arm64-2.337.0.tar.gz
tar xzf runner.tar.gz
./config.sh --url https://github.com/josephvelasquez48/homelab --token <registration-token> --name pi5-runner --labels self-hosted,pi5,arm64 --unattended
sudo ./svc.sh install
sudo ./svc.sh start
```

Runs as user `joe`, which already has `kubectl` configured
(`~/.kube/config`) from cluster setup - no extra credentials needed for the
deploy job.

## Known gap: imperative deploy vs. declarative manifests

`kubectl set image` changes the *running* Deployment directly; it does not
touch `kubernetes/backend/api.yaml` in git. A future `kubectl apply -f
kubernetes/backend/api.yaml` would silently revert the live deployment back
to whatever image tag is checked into that file (`:latest`), undoing the
CI-driven SHA-pinned deploy. This divergence between "what's running" and
"what git says should be running" is exactly the problem GitOps tools
solve - **Argo CD (roadmap step 12)** is the intended fix, making git the
single source of truth instead of CI imperatively pushing changes.

Database migrations are also **not** re-run automatically on every deploy
in this pass - `api-migrate` (`kubernetes/backend/api.yaml`) only runs on
an explicit `kubectl apply`. Auto-running migrations on every push is a
real footgun if not carefully sequenced (schema change before or after the
new code rolls out matters), so it's left as a deliberate manual step
rather than bolted on under time pressure.
