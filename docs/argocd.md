# Argo CD

Roadmap step 12: "Eventually introduce Argo CD so Git becomes the source
of truth for Kubernetes" - directly closes the gap documented in
[docs/cicd.md](cicd.md), where `kubectl set image` changed the running
cluster without touching the checked-in manifest.

## Setup

- Installed via the official manifest, `--server-side` (the plain
  `kubectl apply` failed outright - one CRD, `applicationsets.argoproj.io`,
  exceeds the 262144-byte annotation limit client-side apply hits on large
  CRDs; server-side apply doesn't have that problem).
- `argocd-server` patched to run `--insecure` (plain HTTP, matching every
  other LAN-only service here) - confirmed with the user first, given
  Argo CD's broad cluster-admin-equivalent write access.
- **App of apps**: `kubernetes/argocd/root-app.yaml` is the only manifest
  ever applied by hand. It points at `kubernetes/argocd/apps/`, so every
  actual Application (`namespaces`, `data`, `backend`, `ai`, `monitoring`)
  is itself git-managed - adding or removing a file there is enough to
  have Argo CD take over (or drop) a whole component.
- All Applications use `automated: {prune: true, selfHeal: true}` - this
  is the actual point of the exercise. `selfHeal` means imperative drift
  (anything changed directly against the cluster, bypassing git) gets
  reverted automatically, not just detected.

## Problems hit and fixed

- **Private repo, no credentials**: the first sync attempt failed with
  `authentication required: Repository not found` over HTTPS. Reused the
  read-only deploy key already set up for the Pi's own `git pull` access
  (SSH form, `git@github.com:...`) rather than creating a separate
  credential - same trust boundary, one less thing to manage.

- **`api-migrate` permanently `OutOfSync`**: every other resource in
  `kubernetes/backend/api.yaml` reached `Synced`, but the migration Job
  never would. Root cause: Jobs are immutable once created, so Argo CD
  can't apply an image-tag change to an already-completed Job in place.
  Fixed by making it a `PreSync` hook (`argocd.argoproj.io/hook: PreSync`,
  `hook-delete-policy: BeforeHookCreation`) - Argo CD deletes and recreates
  it fresh on every sync instead of trying to reconcile it, which is the
  correct pattern for one-off Jobs under GitOps generally, not specific to
  this project.

- **ufw's LAN-only rules never applied to K3s at all** - found while
  verifying the deploy loop, not something anticipated going in. `ufw
  status` had no rule for port 80, yet Traefik (fronting `api.home`,
  `grafana.home`, and **`argocd.home`**) worked fine. Checked
  `iptables -L INPUT` and found `KUBE-ROUTER-INPUT`, `KUBE-NODEPORTS`, and
  related chains positioned *before* ufw's own chains - kube-router/
  kube-proxy accept the traffic before ufw's rules ever get evaluated.
  Argo CD specifically being reachable from the whole home network
  (not just conceptually - actually reachable, verified) rather than
  the LAN-scoped access documented and believed to be real was worth
  fixing before moving on, not filing away for the Security phase.

  Fixed with `externalTrafficPolicy: Local` on Traefik's Service (so the
  real client IP survives instead of being SNAT'd to a cluster-internal
  address by `Cluster` policy, the default) plus a `NetworkPolicy`
  restricting Traefik ingress to `192.168.1.0/24` and the IPv6 ULA prefix.
  Applied via `HelmChartConfig` - K3s's supported way to override its
  bundled Traefik chart - not a raw `kubectl patch`, which K3s's own Helm
  controller would silently revert on the next reconcile.

  **Verified enforcement, not just that the resources applied**: spun up a
  throwaway pod on the cluster network (`10.42.x.x`, deliberately outside
  the allowed CIDR) and confirmed it got blocked (`HTTP 000`, connection
  refused/timed out) reaching Traefik's in-cluster Service address, while
  real LAN traffic (`api.home`, `grafana.home`, `argocd.home` from the
  desktop) kept working throughout with zero regression.

  This fix (`kubernetes/argocd/traefik-security.yaml`) is deliberately
  **not** part of the app-of-apps - it configures K3s's own bundled
  Traefik, applied once directly like Argo CD's own install, not
  something that belongs to any of the four workload namespaces.

## The closed loop

```
git push -> GitHub Actions tests -> builds multi-arch image -> pushes to
ghcr.io -> commits the new tag into kubernetes/backend/*.yaml [skip ci]
-> Argo CD's next poll (~3 min, no webhook configured) sees the git
change -> applies it -> selfHeal keeps it that way until the next
legitimate git change
```

Verified for real, not just described: pushed a trivial change, watched
CI build and commit a new SHA tag, watched Argo CD's `backend` Application
go `OutOfSync` -> `Synced` on its own polling cycle, and confirmed the
running pods' image matched the new tag - with zero `kubectl` commands run
by hand and zero use of the self-hosted runner (CI's deploy job now only
needs to write to git, so it moved back to a GitHub-hosted runner).

## Known gaps

- No webhook configured - Argo CD relies on its default ~3-minute polling
  interval to notice git changes, rather than reacting immediately to a
  push. A webhook would need an inbound path from GitHub to the cluster,
  which is exactly the kind of exposure this project has otherwise
  avoided; polling is the trade-off for staying push-free inbound.
- Argo CD's own install (`kubectl apply --server-side -f <upstream URL>`)
  isn't itself tracked as a versioned manifest in this repo - reinstalling
  from scratch means re-running that command with whatever `stable` points
  to at the time, not a pinned version. Fine for a homelab, worth pinning
  a version if this were closer to production.
