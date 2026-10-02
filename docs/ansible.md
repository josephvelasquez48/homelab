# Ansible

Roadmap step 13. Ansible rebuilds the Pi's *host* setup - everything done
by hand over SSH early in the project. Kubernetes manifests are Argo CD's
job ([argocd.md](argocd.md)); managing them here too would mean two systems
fighting over the same state.

## Running it

Two control nodes. **Prefer the Mac**: a control node that is also the
target can't fix the target when it breaks, and the `common` role edits
the Pi's networking.

```bash
# From the Mac, over SSH (the inventory already points at joe@192.168.1.253)
cd ~/Desktop/homelab/ansible
ansible-playbook playbooks/site.yml --check --diff

# From the Pi itself, when the Mac isn't available
cd ~/apps/homelab/ansible
ansible-playbook playbooks/site.yml --check --diff -c local
```

**Two hosts** in `inventory.ini`, both by IP: `pi5` (the Pi, as `joe`) and
`m1-node` (the K3s worker, the Ubuntu VM on the Mac, as `ubuntu`). m1-node
only gets the `k3s` role with `k3s_role: agent` - its pod DNS upstream; the
VM and its join are by hand ([node-migration.md](node-migration.md)). Its
`ubuntu` user accepts the Mac's keys, the Pi's `pi5-ansible` key (the Pi's
`~/.ssh/config` uses it for `192.168.1.63` only - not the GitHub deploy
key) and the desktop's.

From the Pi, `-c local` is for the Pi alone; m1-node goes over SSH, so run
them separately:

```bash
ansible-playbook playbooks/site.yml --tags k3s --limit pi --check --diff -c local
ansible-playbook playbooks/site.yml --tags k3s --limit m1 --check --diff
```

Always `--check --diff` first. The two nodes run different Ansible
versions (ansible-core 2.19 on the Pi from apt, 2.21 on the Mac from
Homebrew), so check from the node you'll apply from. macOS has no
`timeout` command, so scripts that wrap the playbook in it only work on
the Pi.

## Roles, in order

| Role | What it does |
|---|---|
| `common` | Memory cgroup kernel flag (plus reboot), the Pi's own DNS resolver, avahi's `enable-wide-area=no`; turns off what the desktop image ships but nothing uses - printing (CUPS, `cups-browsed`), the Bluetooth MPRIS proxy - and removes the retired blocklist timer |
| `docker` | Docker Engine |
| `firewall` | ufw: LAN-only rules for SSH, DNS, K3s; removes rules for retired IPv6 prefixes |
| `k3s` | K3s server, and both nodes' pod DNS upstream (`k3s_role: agent` for m1-node) |
| `dns_monitoring` | The repo checkout and the CoreDNS + AdGuard Compose stack |

Each depends on the one before it: Docker needs the cgroup fix, K3s needs
Docker's iptables setup.

The `github_runner` role (the self-hosted Actions runner) is no longer in
`site.yml`: the runner was removed on 2026-09-30 - see
[cicd.md](cicd.md). Its own playbook, `playbooks/github-runner.yml`, says
when and how to reinstall it.

## Safeguards built in

- **No ufw rules for Traefik's 80/443.** K3s's iptables run first, so they
  would do nothing while looking like protection; the real boundary is a
  NetworkPolicy ([argocd.md](argocd.md)).
- **Network restarts are opt-in.** Reactivating the Pi's connection drops
  DNS for the whole LAN for a few seconds. The role writes the config (it
  applies at next reboot) and only restarts the connection with
  `-e pi_allow_network_bounce=true`; otherwise it prints that a change is
  staged.
- **Connections found by device, not name**, since the name changed when
  the Pi moved to Ethernet. It fails loudly if nothing is active on
  `pi_lan_interface`.
- **Stale firewall rules are removed explicitly.** ufw doesn't delete a rule
  just because Ansible stopped asking for it. `retired_ipv6_prefixes` lists
  old prefixes (like the previous router's ULA) whose rules get deleted -
  a stale allow-rule reads as protection that isn't there.

## Bugs found by actually running it

| Bug | Fix |
|---|---|
| `--check` skipped the cgroup check, so the diff showed the kernel flags appended twice | `check_mode: false` on that read-only task |
| `ansible_architecture == 'aarch64' \| ternary(...)`: the filter binds before `==`, so it was always true | Parentheses |
| git refused to pull: a script had been `chmod +x`'d on the Pi but tracked as 644 | `git update-index --chmod=+x`, fixed in the repo |
| `nmcli` escapes colons (`\:\:1`), so the "already set" check never matched and every run would bounce the network | `--escape no` |
| git refused to pull again: a file had been copied to the Pi with `scp` | Commit, merge, pull - never `scp` things you mean to keep |

**Idempotency verified, not assumed:** a real run, then a second real run
reporting `changed=0`. Same after the 2026-09-11 changes: check run, real
run (`changed=3`), DNS and both nodes confirmed untouched, second run
`changed=0`.

## History

The first control node was the WSL2 Ubuntu distro, retired with the node
migration ([node-migration.md](node-migration.md)); for a while nothing
could run the playbook. The Pi became a control node on 2026-09-10, and
the Mac on 2026-09-11. (Running from a Windows drive mounted in WSL also
silently ignored `ansible.cfg`: NTFS permissions look world-writable, so
Ansible refuses the config.)
