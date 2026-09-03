# Ansible

Roadmap step 13: "Use Ansible for server configuration."

## Scope

Codifies the Pi's **host-level** setup - everything that was done by hand
over SSH earlier in this project: the cgroup kernel param fix, Docker
install, `ufw` rules, K3s install, the two remaining Docker Compose stacks
(DNS, monitoring), and the self-hosted GitHub Actions runner.

Deliberately **not** Kubernetes manifests - Argo CD ([docs/argocd.md](argocd.md))
already owns those declaratively. Re-deploying them via Ansible too would
just be two systems fighting over the same state, with no clear owner of
"what's actually true" when they disagree.

## Control node

Ansible doesn't run natively on Windows - it needs a POSIX control node.
Used the Ubuntu-24.04 WSL2 distro already set up as a K3s worker node
(`docs/kubernetes.md`) rather than standing up something new, targeting
the Pi over the same SSH access pattern used everywhere else in this
project (a dedicated keypair, added to `authorized_keys`).

**Working copy matters**: running Ansible directly against the repo's
`/mnt/d/homelab/ansible` (the Windows-drive WSL2 mount) silently ignored
`ansible.cfg` - `/mnt/*` mounts don't map NTFS permissions cleanly, so
Ansible's world-writable-directory safety check flags the whole path.
Copying the working files into WSL2's own filesystem first (`~/homelab-ansible`)
fixed it. A real deploy pipeline would `git clone` there instead of copying
from a Windows mount, which only exists because this session runs from
Windows.

## Roles

`common` (cgroup fix + reboot) -> `docker` -> `firewall` -> `k3s` ->
`dns_monitoring` -> `github_runner`, in that order (each depends on the
one before: Docker needs the cgroup fix to have already happened, K3s
needs Docker's iptables setup in place, etc.).

`firewall`'s rule set is deliberately incomplete in a way that matches
reality, not aspiration: it does **not** open port 80/443 for Traefik,
because a `ufw` rule there would be a no-op that implies protection it
doesn't provide - see the K3s-bypasses-ufw finding in
[docs/kubernetes.md](kubernetes.md) and its fix in
[docs/argocd.md](argocd.md). Writing a rule that looks like security but
isn't would be worse than no rule at all.

`github_runner` needs a registration token that expires within the hour,
so it can't be baked into the playbook - passed at run time:

```bash
ansible-playbook playbooks/site.yml --tags github_runner \
  -e runner_token=$(gh api repos/josephvelasquez48/homelab/actions/runners/registration-token --jq .token)
```

## Log

- 2026-09-03: Wrote all six roles, then actually ran them - `--check
  --diff` first, catching **three real bugs** before they ever touched the
  live Pi:
  1. The cgroup-check task got skipped in `--check` mode (Ansible defaults
     read-only `command` tasks to skip during a dry run), so the "is this
     already present" condition saw no data and tried to double-append the
     kernel params - visible directly in the diff output
     (`cgroup_memory=1 ... cgroup_memory=1 ...`, duplicated). Fixed with
     `check_mode: false` on that one task - a read-only check is safe to
     actually run even during `--check`.
  2. A Jinja operator-precedence bug in the Docker role's architecture
     detection: `ansible_architecture == 'aarch64' | ternary('arm64',
     'amd64')` - the `|` filter binds tighter than `==`, so it evaluated
     `'aarch64' | ternary(...)` first (a non-empty string, always truthy)
     and then compared *that* against `ansible_architecture`, instead of
     the intended comparison. Confirmed by diffing against the actual
     correct `arch=arm64` line already on the Pi from the original manual
     install. Fixed with explicit parentheses.
  3. The `git` module refused to pull with `Local modifications exist in
     the destination (force=no)` - correct, safe behavior, not a bug in
     Ansible. Root cause: `docker/dns/scripts/update-blocklist.sh` had
     been `chmod +x`'d by hand on the Pi (more than once this session -
     see docs/kubernetes.md for the first time this exact issue appeared)
     but git had only ever tracked it as mode `644`, so every deploy left
     an untracked local modification blocking the next pull. Fixed at the
     actual root this time: `git update-index --chmod=+x` + commit, so the
     executable bit is correctly part of what git delivers on every future
     clone/pull - not a workaround in the playbook, a fix to the repo.

  After all three fixes: a real (non-check) run applied cleanly (2
  legitimate, safe permission-tightening changes - `.kube` from `0775` to
  the declared `0700`, the blocklist script from `0775` to `0755`), and a
  **second** real run immediately after reported `changed=0` - genuine,
  verified idempotency, not assumed. Confirmed the whole stack (K3s nodes,
  `api.home`, `grafana.home`, Docker containers on the Pi) stayed healthy
  throughout.
