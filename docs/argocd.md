# Argo CD

Roadmap step 12: git is the source of truth for the cluster. CI commits
new image tags; Argo CD applies whatever is in git and reverts anything
changed by hand.

## How it's set up

- **App of apps.** `kubernetes/argocd/root-app.yaml` is the only manifest
  applied by hand. It points at `kubernetes/argocd/apps/`, one Application
  per area: `namespaces`, `data`, `backend`, `ai`, `monitoring`, `chat`,
  `dashboard`, `kiwix`. Adding a file there is all it takes to put a
  component under GitOps.
- **Automated sync with `prune` and `selfHeal`**, so drift is reverted,
  not just reported. Self-heal measured at ~11 s
  ([failure-testing.md](failure-testing.md)).
- **Installed with `kubectl apply --server-side`**: one CRD is too big for
  client-side apply's annotation limit.
- **Repo access** with the Pi's existing read-only deploy key.
- **`argocd-server --insecure`**: TLS ends at Traefik (`argocd.home`,
  [https.md](https.md)); only the hop inside the cluster is HTTP.
- **Unused components scaled to 0** (2026-09-30, about 66 MB back on the
  Pi): `argocd-dex-server` (single sign-on - none configured, the UI uses
  the admin login), `argocd-notifications-controller` (no notifications
  set up; cluster alerts go through Alertmanager) and
  `argocd-applicationset-controller` (no ApplicationSets - the app of apps
  above is hand-written). A live change, like the repo-server pin below,
  so reapply it after a reinstall. To bring one back:
  `kubectl -n argocd scale deploy argocd-dex-server --replicas=1`.
- **Not managed by Argo CD:** Argo CD's own install, the Traefik overrides
  in `kubernetes/argocd/traefik-security.yaml`, and the secrets
  ([secrets.md](secrets.md)).

## The deploy loop

```
git push -> CI tests -> builds the image -> pushes to ghcr.io
         -> commits the new tag to kubernetes/ [skip ci]
         -> Argo CD sees it on its next poll (~3 min) -> applies it
```

Verified end to end: a trivial change went from push to running pods
with no `kubectl` by hand. CI only writes to git, so it needs no cluster
access.

## Problems found and fixed

| Problem | Cause | Fix |
|---|---|---|
| Traefik (and so Argo CD) reachable from the whole network, despite ufw's LAN-only rules | K3s's iptables chains run *before* ufw's, so ufw never saw the traffic | `externalTrafficPolicy: Local` (keeps the client IP) and a NetworkPolicy allowing only the LAN, via K3s's `HelmChartConfig`. Verified with a pod outside the allowed range: blocked, while the LAN kept working |
| `api-migrate` Job stuck `OutOfSync` | Jobs are immutable, so a new image tag can't be applied | Made it a `PreSync` hook, recreated on every sync |
| Every Application failing with `DeadlineExceeded` | A wedged git fetch holds the per-repo lock; restarted, repo-server landed on the flaky WSL2 node and failed its liveness probe | Pinned repo-server to the Pi (a live patch - reapply after a reinstall) |
| Changed Endpoints never applied, while showing `Synced` | Argo CD excludes `Endpoints` by default | See [kubernetes.md](kubernetes.md) |

## Known gaps

- **Polling, no webhook.** Changes take up to ~3 minutes. A webhook would
  need a path from GitHub into the LAN, which nothing else here has.
- **Argo CD's install isn't pinned** in this repo; a reinstall takes
  whatever `stable` is at the time.
- **The admin password** is still the install-time one. It's backed up,
  encrypted, in `kubernetes/secrets/reference/argocd-admin.enc.yaml`
  ([secrets.md](secrets.md)); `argocd-initial-admin-secret` still exists
  and can be deleted.
