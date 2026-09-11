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
That was the Ubuntu-24.04 WSL2 distro, which was retired during the node
migration (`docs/node-migration.md`). Nobody noticed at the time, so from
then until 2026-09-10 the playbook had nowhere to run from at all.

**The Pi is now the control node, running against itself:**

```bash
cd ~/apps/homelab/ansible
ansible-playbook playbooks/site.yml --check --diff -c local
```

`-c local` is required. The inventory addresses the Pi over SSH, which is
correct from any other control node, but the Pi holds no key authorising
it to connect to itself - and going through localhost SSH would be a
pointless hop anyway. `become` needs no password here: `joe` has
`NOPASSWD: ALL`.

Control node and target being the same host is a real weakness, not a
tidy solution. A change that breaks the Pi's networking also breaks the
thing that would fix it, and the `common` role now edits exactly that.
The alternative is installing Ansible on the MacBook, which is the better
shape but adds a second machine that has to be present and current before
anything can be provisioned.

**Historical, kept because the failure was non-obvious**: running Ansible
directly against the repo's `/mnt/d/homelab/ansible` (the Windows-drive
WSL2 mount) silently ignored `ansible.cfg` - `/mnt/*` mounts don't map
NTFS permissions cleanly, so Ansible's world-writable-directory safety
check flagged the whole path. Copying into WSL2's own filesystem fixed
it. Moot now: the Pi runs from a real `git clone`, which is what that
entry recommended.

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

- 2026-09-10: **Control node moved to the Pi, and two DNS host settings
  moved into the `common` role.** Both settings had been applied by hand
  during the DNS incident that night (`docs/dns-loop.md`) and existed
  nowhere else, so a rebuild from this repo would have come back without
  them:

  1. **`enable-wide-area=no`** in `/etc/avahi/avahi-daemon.conf`. Left on,
     avahi queries `lb._dns-sd._udp.<reverse-subnet>.in-addr.arpa` over
     unicast DNS at roughly 13 per second, all failing, all forwarded to
     the upstream resolvers.
  2. **The Pi's own resolver**, pointed at `127.0.0.1`/`::1` with
     `ignore-auto-dns` on both families. Without the second half,
     NetworkManager appends the DHCP- and RA-learned servers and the Pi
     resolves through whichever answers first.

  The role looks the NetworkManager connection up **by device**, not by
  name - that name changed when the Pi moved from Wi-Fi to Ethernet the
  same night - and fails loudly when nothing is active on
  `pi_lan_interface` rather than silently configuring nothing.

  **One bug, found by running the command instead of assuming its output.**
  `nmcli --get-values` escapes colons, returning the IPv6 address as
  `\:\:1`. The idempotency comparison against `::1` would never have
  matched, so the task would have re-run and reactivated the connection on
  every single play - and reactivating drops the only resolver on the LAN
  for several seconds. `--escape no` fixes it.

  Verified with `--check --diff -c local`: 27 ok, 0 failed, and the
  resolver task **skips**, which is the assertion that matters - the guard
  works and re-runs will not bounce the network.

  Two tasks report `changed` in check mode, both understood and neither a
  defect:

  - `firewall: Allow LAN-scoped services (IPv6)` - the live ufw rule for
    the current prefix was added by hand without a comment, and ufw treats
    the comment as part of the rule identity.
  - `dns_monitoring: Clone or update the homelab repo` - the Pi's checkout
    was on a feature branch during the test, so the role wanted main back.

  **Also still on the box and matching nothing**: ufw rules scoped to
  `fd00:f405:95c7:c412::/64`, the dead ULA prefix from the old router. The
  role no longer emits them (`lan_ipv6_prefix` is the delegated GUA now),
  but `ufw` does not remove a rule just because Ansible stopped asking for
  it. A stale allow-rule reads as protection that is not being provided,
  which is the exact mistake the firewall role's own comments refuse to
  make elsewhere. Not cleaned up yet.

  The Pi being both control node and only target is a weakness worth
  stating: a change that breaks its networking also breaks the thing that
  would fix it, and this role now edits precisely that.

- 2026-09-11: **Gated the network bounce, and taught the firewall role to
  clean up after itself.**

  Making the Pi the control node the night before turned the resolver task
  into a trap. It fired its handler unconditionally, so any routine play
  would reactivate the connection and drop DNS for every device on the LAN
  for several seconds - on the one host that is simultaneously the only
  resolver, the K3s control plane, and the machine running the play.

  Writing the NetworkManager profile is harmless and persists, so the
  config still converges and corrects itself at the next reboot. Only the
  disruptive half is now opt-in:

  ```bash
  ansible-playbook playbooks/site.yml -c local -e pi_allow_network_bounce=true
  ```

  Without it, a staged-but-unapplied change prints a message saying so
  rather than silently leaving `/etc/resolv.conf` stale. The handler is
  gone - an explicit task puts the condition where someone reading the
  role will actually see it, which a `notify:` line does not.

  **`ufw` does not remove a rule just because Ansible stopped asking for
  it.** Dropping a prefix from `lan_ipv6_prefix` only stops the rule being
  created. The allow-rules for `fd00:f405:95c7:c412::/64` - the previous
  router's self-generated ULA - had been sitting there matching nothing
  ever since the router was replaced. A stale allow-rule is worse than no
  rule, because it reads as protection that is not being provided, which
  is the mistake this role's own comments refuse to make for Traefik's
  ports. `retired_ipv6_prefixes` now deletes them explicitly.

  Deleting is safe *because* nothing holds an address in that prefix. If
  something did, the rule would be load-bearing and removal would be the
  wrong fix - so the list is a deliberate record of retired prefixes, not
  a diff against the live one.

  Verified in the order the earlier entries in this log argue for: check
  run first (both new tasks skipped, the two firewall changes reported),
  then a real run (`changed=3`), then confirmation that `resolv.conf`, the
  interface, DNS on both families and both cluster nodes were untouched,
  then a **second real run reporting `changed=0`**. The firewall ended at
  eight rules, all commented, no dead prefixes.
