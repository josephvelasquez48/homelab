# HTTPS

TLS ends at Traefik with a certificate from the homelab's own CA; port 80
redirects to 443. The hop from Traefik to each pod stays HTTP (so Argo CD
keeps `--insecure` internally).

## Status

| Hostnames | HTTPS |
|---|---|
| `api.home`, `ai.home`, `dashboard.home`, `grafana.home`, `argocd.home` | **Valid** - verified with certificate checking on |
| `chat.home`, `wikipedia.home`, `alerts.home`, `prometheus.home` | **Broken** - Traefik falls back to its self-signed default, so browsers warn. The certificate only lists the first five names (`certificates/issue.py`), and the `chat` and `kiwix` namespaces have no copy of the TLS Secret |
| `phone.home:8443` | Valid - its own certificate ([phone.md](phone.md)) |
| AdGuard's UI (`:3000`), Ollama on the desktop | Plain HTTP - outside Traefik |

**To fix the broken four:** add them to `hosts` in `certificates/issue.py`,
reissue (steps below), and add `chat` and `kiwix` to the namespaces
`homelab-tls.enc.yaml` creates the Secret in.

## The CA and trust

- **Public root:** `certificates/homelab-ca.crt`. Name-constrained to
  `.home`, so it can't sign anything else. Valid ten years.
- **Private key:** outside git, in the restricted age-key folder on
  Windows, with a SOPS-encrypted copy in
  `kubernetes/secrets/reference/homelab-ca.enc.yaml`. It's never in
  Kubernetes. Protect the age key too - the encrypted copy is useless
  without it.
- **Trust the root on each client** - never turn off verification instead.
  Done on Windows (current user) and on the Pi; not yet on the Mac or
  phones.

```powershell
Import-Certificate -FilePath D:\homelab\certificates\homelab-ca.crt -CertStoreLocation Cert:\CurrentUser\Root
```

`kubernetes/secrets/homelab-tls.enc.yaml` holds the server key and
certificate as a list of namespace-local Secrets, applied with the normal
SOPS process ([secrets.md](secrets.md)).

## Renewing (manual - expires 2027-09-10)

There's no cert-manager and no expiry alert yet. Renew with the *same* CA,
so clients don't need a new root:

1. Run `certificates/issue.py` (needs Python `cryptography`, SOPS, and the
   CA key and certificate). It makes a new server key and encrypted Secret
   manifests - not a new CA.
2. Apply `homelab-tls.enc.yaml` with `kubernetes/secrets/apply.sh`.
3. Check every hostname with verification on, then commit the encrypted
   files. Never commit plaintext keys.

## Worth knowing

- **Traefik is pinned to the Pi.** DNS points at the Pi and
  `externalTrafficPolicy: Local` needs a local Traefik pod; a rollout that
  put it on the M1 broke ingress until it was pinned.
- **Windows `curl` needs `--ssl-revoke-best-effort`** for this CA, since
  there's no revocation service. That still checks the chain and hostname
  - it isn't `--insecure`.
- **API clients should use `https://api.home` directly**, not rely on the
  redirect after already sending an API key over HTTP. Health probes and
  in-cluster calls stay on internal HTTP.
- **The dashboard's session cookie** is Secure, HttpOnly, SameSite=Strict -
  verified with a real login.
