# HTTPS for homelab services

TLS terminates at Traefik for `api.home`, `ai.home`, `dashboard.home`,
`grafana.home`, and `argocd.home`. Port 80 redirects to HTTPS on port 443.
The internal Traefik-to-service hop remains HTTP; this is not pod-to-pod TLS.
In particular, Argo's `--insecure` setting remains for its internal HTTP service.

## Certificates and trust

`certificates/homelab-ca.crt` is the public local root. Its DNS name constraint
limits issuance to `.home`; server SANs list the five service names explicitly.
Trust the root on each client before using the HTTPS names. Windows current-user
Root and the Pi system trust store were configured during deployment. Other
clients, including phones and browsers with separate certificate stores, need
manual trust. Do not disable certificate verification as a workaround.

Windows import:

```powershell
Import-Certificate -FilePath D:\homelab\certificates\homelab-ca.crt -CertStoreLocation Cert:\CurrentUser\Root
```

The CA private key is outside git at
`C:\Users\josep\.config\sops\age\homelab-ca.key`, inside the restricted key
directory. A SOPS-encrypted reference is in
`kubernetes/secrets/reference/homelab-ca.enc.yaml`. The CA key is not installed
in Kubernetes. `kubernetes/secrets/homelab-tls.enc.yaml` holds a List of four
namespace-local TLS Secrets; these contain the shared server key and certificate
and follow the existing manual SOPS apply process.

## Renewal

The server certificate expires September 10, 2027. Renewal is manual; there is
no cert-manager or expiry alert yet. Renew before expiry using the existing CA
so clients do not need to trust a replacement root:

1. Run `certificates/issue.py` using Python with `cryptography` and SOPS installed.
   The existing CA key and public certificate must both be present. The script
   generates a new server key and encrypted secret manifests, not a new CA.
2. Decrypt/apply `homelab-tls.enc.yaml` through the existing secrets workflow.
3. Verify all five HTTPS hosts with certificate verification enabled, then commit
   the encrypted changes. Do not commit plaintext key files.

The CA lasts ten years. Protect the age key as well as the CA key; the encrypted
reference alone cannot be decrypted without the age identity.

## Deployment and clients

Workload Ingresses are GitOps-managed. Argo's own Ingress and
`kubernetes/argocd/traefik-security.yaml` must still be applied manually.
Install TLS Secrets before switching the Ingresses and redirect.

The dashboard session cookie is Secure, HttpOnly, SameSite=Strict. Tests use an
HTTPS base URL. Health probes and cluster-internal service calls continue to
use their internal HTTP URLs. LAN API clients should change their base URL to
`https://api.home` rather than relying on a redirect after sending an API key
in an HTTP request. Load-test scripts with explicit HTTP URLs need an HTTPS
base URL and trust of the root on their execution host.

AdGuard's separate port-3000 UI is outside these Ingresses and remains HTTP.
This rollout does not add encryption to that UI or Ollama's LAN API.

Reference: https://doc.traefik.io/traefik/reference/routing-configuration/kubernetes/ingress/

## Live verification

All five names passed certificate/hostname validation. HTTP returns 308 with
an HTTPS Location. A real dashboard login confirmed Secure, HttpOnly and
SameSite=Strict cookies, and the test session was logged out afterward.
Traefik is pinned to `joe`: DNS points to the Pi, and `externalTrafficPolicy:
Local` requires a local Traefik pod. A rollout initially scheduled Traefik on
the M1 and interrupted ingress until this placement was corrected.

Windows curl with Schannel requires `--ssl-revoke-best-effort` for this CA
because no CRL/OCSP service is configured. This preserves chain/hostname
verification; it is not `--insecure`. Windows PowerShell HTTPS validation and
Python validation with the explicit CA both passed without disabling trust.

The encrypted CA reference and public root are included in the Mac's recovery
backup, verified by a full Restic data check. Mac/browser trust installation
has not been performed; the recovery directory contains the public certificate.
