# Secrets management

Roadmap step 15 (Security). The cluster's `Secret` objects
(`postgres-credentials`, `api-secrets`, `grafana-admin`) used to live as
plaintext manifests alongside everything else in `kubernetes/`, committed to
a public GitHub repo. Moved them to SOPS-encrypted files, applied out-of-band
instead of through Argo CD.

## Why SOPS + age, and why not KSOPS

[SOPS](https://github.com/getsops/sops) encrypts individual YAML/JSON values
in place, keyed by [age](https://github.com/FiloSottile/age) - the file
stays diffable and reviewable in git (keys visible, values encrypted), and
`sops --decrypt` is a single dependency-free binary. Chosen over
alternatives like Sealed Secrets or full Vault because the cluster is a
single small homelab, not something that needs a running secrets-management
service of its own.

Argo CD has a plugin path for this - **KSOPS** - that would let it decrypt
and sync `Secret` manifests directly, keeping them in the normal GitOps
flow. Deliberately not set up: KSOPS needs a custom Argo CD repo-server
image (a plugin sidecar baked in), which is real extra surface area and
maintenance for three secrets that rotate rarely. Instead, encrypted files
live in `kubernetes/secrets/*.enc.yaml`, decrypted and applied by hand (or
via `kubernetes/secrets/apply.sh`) whenever they change, and are explicitly
**excluded** from anything Argo CD tracks - see "The ordering rule" below
for why that exclusion has to be real, not just a convention.

## Setup

- Age keypair generated once, private key kept at
  `~/.config/sops/age/keys.txt` (Linux) / `%APPDATA%\sops\age\keys.txt`
  (Windows) - never committed. Public key
  (`age1tsns9fmhenrdl5ufs2vs28gut2m464xlcp23440uxp4x3aqvdgzsytyjwx`) is safe
  to commit and lives in `.sops.yaml`:

  ```yaml
  creation_rules:
    - path_regex: kubernetes/secrets/.*\.enc\.yaml$
      key_groups:
        - age:
            - age1tsns9fmhenrdl5ufs2vs28gut2m464xlcp23440uxp4x3aqvdgzsytyjwx
  ```

- **Gotcha**: the creation rule matches against the *input* file's path,
  not wherever the output gets redirected to. Encrypting a scratch file
  into `kubernetes/secrets/foo.enc.yaml` via `sops --encrypt scratch.yaml >
  kubernetes/secrets/foo.enc.yaml` doesn't trigger the rule, since as far
  as SOPS is concerned the input path is `scratch.yaml`. Worked around by
  passing `--config /dev/null --age <pubkey>` explicitly instead of relying
  on path-based rule matching for anything not already sitting at its
  final path.

- `kubernetes/secrets/apply.sh` decrypts and `kubectl apply`s every
  `*.enc.yaml` in the directory:

  ```bash
  for f in "$SCRIPT_DIR"/*.enc.yaml; do
      sops --decrypt "$f" | kubectl apply -f -
  done
  ```

  Deliberately **not** wired into Argo CD or CI - it's a manual/on-call
  action, not something that should run unattended against production
  credentials.

## The ordering rule (learned the hard way)

The plaintext `Secret` blocks were removed from
`kubernetes/{data/postgres,backend/api,monitoring/grafana}.yaml`, replaced
with a comment pointing at the encrypted file. That removal is itself a git
change Argo CD's `selfHeal` will act on - which creates a real ordering
hazard when rotating a live credential at the same time:

**What happened**: rotated all three passwords, ran `apply.sh` to push the
new values into the cluster - *before* pushing the manifest change that
removes the plaintext `Secret` from git. From Argo CD's point of view,
nothing had changed yet: the old plaintext `Secret` was still the tracked
desired state, so `selfHeal: true` silently reverted `api-secrets` back to
the old password within its next reconcile. Postgres's actual password
*had* rotated (`ALTER USER` had already run - see below), so the live
credential and the reverted `Secret` now disagreed, and the next
`kubectl rollout restart` crash-looped every new `api`/`worker` pod with
`password authentication failed for user "homelab"`.

**The fix is sequencing, not tooling**: push the manifest change (removing
the `Secret` from what Argo CD tracks) *first*, confirm Argo CD has
actually synced that commit, and only then apply the new secret values.
Once `api-secrets` is no longer in the desired manifests at all, Argo CD
stops treating it as a resource it owns - re-applying it via `apply.sh`
(plain `kubectl apply`, no Argo CD annotations in the payload) leaves it
with no `argocd.argoproj.io/tracking-id` annotation, confirmed by diffing
`kubectl get secret api-secrets -o yaml` before and after. Without that
annotation Argo CD can neither revert it via `selfHeal` nor flag it for
pruning - which is the actual mechanism keeping "applied out-of-band" true
going forward, not just a one-time fix.

This one mistake cascaded into a second, unrelated-looking failure: the
`api-migrate` `PreSync` hook `Job` kept retrying against the stale
credential until it hit `backoffLimit`, which blocked the *entire* sync
(hooks run before the main sync resources) - so `data`/`monitoring`'s
already-pruned `postgres-credentials`/`grafana-admin` `Secret`s stayed
missing from the live cluster far longer than intended, and `backend`
stayed `OutOfSync` even after the credential itself was fixed, until the
Job was deleted so the hook could recreate it. Full incident, including a
second, genuinely unrelated networking bug uncovered along the way, is in
[docs/kubernetes.md](kubernetes.md).

## Rotating a live credential

Rotating the `Secret` object is necessary but not sufficient. Postgres and
Grafana both only apply their admin-password environment variable on
**first initialization** - once the data volume exists, changing the env
var does nothing to the already-running service:

- **Postgres**: `ALTER USER homelab WITH PASSWORD '<new>';` via `psql`
  against the live pod.
- **Grafana**: `grafana cli admin reset-admin-password` looks like the
  obvious tool, but it spawns a second process alongside the running
  server and OOM-killed itself against the container's 256Mi limit (exit
  137). Used the HTTP Admin API instead -
  `PUT /api/admin/users/1/password`, authenticated with the still-valid
  *old* password - which doesn't need extra memory since it runs in the
  existing server process.
- **API secrets** (`API_KEY`, `DATABASE_URL`, `REDIS_URL`): these are
  read fresh from the environment on every pod start, so rotating the
  `Secret` and doing a `kubectl rollout restart` is sufficient - no
  service-side "first init only" quirk to work around.

Verified after each rotation: old credential explicitly rejected (not just
"didn't check"), new credential works end-to-end through the real
endpoint - `grafana.home` login, `api.home/health` reporting
`"postgres":"ok"`.

## Known gaps

- No KSOPS - secrets are outside Argo CD's normal reconciliation entirely,
  which means a `kubectl apply -f kubernetes/` from a clean checkout
  doesn't produce a working cluster on its own; `apply.sh` has to be run
  too. Acceptable for a single-operator homelab, would need KSOPS (or
  equivalent) to be a real multi-operator GitOps setup.
- No automatic drift detection on the encrypted secrets themselves - if
  someone changes a live `Secret` by hand and forgets to re-encrypt/update
  `kubernetes/secrets/*.enc.yaml`, the two silently diverge with nothing
  flagging it.
