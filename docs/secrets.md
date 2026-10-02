# Secrets

Roadmap step 15 (Security). Kubernetes Secrets used to be plaintext
manifests in a public repo. They're now encrypted with SOPS + age in
`kubernetes/secrets/*.enc.yaml` and applied by hand, outside Argo CD.

## How it works

- **SOPS + age** encrypts just the values, so files stay readable and
  diffable in git. The age private key lives outside the repo
  (`~/.config/sops/age/keys.txt`, `%APPDATA%\sops\age\keys.txt` on
  Windows); the public key is in `.sops.yaml`.
- **Applied with `kubernetes/secrets/apply.sh`**, which decrypts every
  `*.enc.yaml` there and `kubectl apply`s it. Run by hand when a secret
  changes - never from CI or Argo CD.
- **Why not KSOPS** (Argo CD decrypting them itself): it needs a custom
  Argo CD image, which is too much machinery for a handful of secrets
  that rarely change.

Encrypted secrets: `postgres-credentials`, `api-secrets`, `grafana-admin`,
`chat-secrets`, `homelab-tls`, `alertmanager-config`,
`adguard-exporter`, `cam-secrets`.

## The ordering rule

**Take a secret out of Argo CD's manifests first, and wait for that sync,
before applying a new value.**

Learned the hard way: all three passwords were rotated and applied
*before* the commit that removed the plaintext Secrets from git. Argo CD
still considered the old Secret its desired state, so `selfHeal` put the
old password back. Postgres already had the new one, so every restarted
`api` pod failed to log in. The failing migration hook then blocked the
whole sync, which kept other Secrets missing longer.

Once a Secret isn't in any manifest, Argo CD doesn't own it: applied
with plain `kubectl apply`, it has no Argo tracking annotation, so Argo
can neither revert nor prune it.

## Rotating a credential

Changing the Secret isn't enough where a service only reads its password
on first start:

| Credential | After updating the Secret |
|---|---|
| Postgres | `ALTER USER homelab WITH PASSWORD '...'` in the running pod |
| Grafana | The admin API: `PUT /api/admin/users/1/password` (the `grafana cli` reset OOM-killed itself) |
| API secrets | `kubectl rollout restart` - read on every start |

After each rotation, check that the old password is *rejected* and the
new one works through the real endpoint.

## Reference copies

Some credentials can't be applied from a file, because the service keeps
its own hash. Their encrypted copies just record the credential, for a
rebuild:

| File | What |
|---|---|
| `kubernetes/secrets/reference/argocd-admin.enc.yaml` | Argo CD admin (password and bcrypt hash) |
| `kubernetes/secrets/reference/homelab-ca.enc.yaml` | The homelab CA key ([https.md](https.md)) |
| `docker/dns/secrets/adguard-admin.enc.yaml` | AdGuard Home admin |

`apply.sh` doesn't read `reference/`, on purpose. Argo CD's `argocd-secret`
also holds the session-signing key and TLS material, so applying a
partial copy would wipe them. Restore the password with a *patch*
instead:

```bash
HASH=$(sops --decrypt kubernetes/secrets/reference/argocd-admin.enc.yaml | awk '/^bcrypt_hash:/{print $2}')
kubectl -n argocd patch secret argocd-secret --type merge \
  -p "{\"stringData\":{\"admin.password\":\"$HASH\",\"admin.passwordMtime\":\"$(date -u +%FT%TZ)\"}}"
kubectl -n argocd rollout restart deploy argocd-server
```

## Gotchas

- SOPS matches `.sops.yaml` rules against the *input* path, so encrypting
  a scratch file into place doesn't pick up the rule. On Windows, rule
  matching failed outright. Both are avoided by passing
  `--config /dev/null --age <public key>` explicitly.

## Known gaps

- A fresh cluster needs `apply.sh` as well as Argo CD; the secrets aren't
  in the GitOps loop.
- Nothing notices if a live Secret is changed by hand and the encrypted
  file isn't updated.
