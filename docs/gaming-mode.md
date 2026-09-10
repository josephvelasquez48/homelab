# Gaming mode

**Rewritten 2026-09-10.** What this used to be, and what it is now:

| | Before | Now |
|---|---|---|
| Trigger | `POST /api/gaming/on` and `/off` | `POST /api/gpu/release` |
| Mechanism | SSH to the desktop, run PowerShell, `kubectl cordon` + `drain` + stop `k3s-agent` | One HTTP call to Ollama's own API |
| Reverse action | `postgame.ps1` to uncordon and rejoin | None needed |
| Requires | A mounted SSH private key, a `known_hosts` ConfigMap, remote PowerShell | Nothing new - the dashboard already reaches the inference Service |

The premise changed rather than the goal. The desktop was both the K3s
worker and the gaming rig, so a game competed with `api`/`worker` pods for
CPU and with Ollama for VRAM. The worker moved to an M1 MacBook and the
desktop left the cluster entirely ([node-migration.md](node-migration.md)),
so the CPU half of that contention no longer exists and there is no node
to cordon.

What is left is only VRAM: Ollama holds ~4.7GB of the 3070 Ti's 8GB while
a model is resident. `POST /api/generate` with `keep_alive: 0` and no
prompt evicts it - Ollama returns `done_reason: unload` and generates
nothing, confirmed against v0.32.5 rather than taken from the docs.

**There is no "off" any more, and that is not an omission.** The next
inference request reloads the model on demand, so a second button would
have been a no-op with a reassuring label. Ollama also evicts on its own
after an idle period, which means this only makes the release immediate
rather than eventual - a convenience, and worth being honest that it is
one.

**The security surface that went with it** was the largest in the project:
a pod holding an SSH private key that executed PowerShell on a Windows
host as an admin user, which [security-testing.md](security-testing.md)
ranked as its highest-impact finding. The session auth stays - this is
still a state change that should not be triggerable cross-site - but the
key, the host key handling, the `ssh_runner`, and both `.ps1` scripts are
deleted.

The rest of this document describes how it worked before the migration,
kept because the bug it records is still worth reading.

---

Not part of the original 18-step roadmap - added afterward. The desktop
was both the K3s worker node and the gaming rig, so launching a game
competed with `api`/`worker` pods for CPU, and with Ollama for GPU.
`gaming-mode/pregame.ps1` and `gaming-mode/postgame.ps1` cleanly removed
the desktop from the cluster before gaming and brought it back after.

## Why a manual trigger, not automatic game detection

Asked rather than assumed, since it fundamentally changes the design:
automatic detection (watching for specific game processes, or a generic
GPU-usage/fullscreen heuristic) either only covers pre-registered games
or is inherently unreliable (false positives from other GPU-heavy apps,
false negatives from borderless-windowed games). A manual pair of
scripts - run before launching, run after closing - is simple, has zero
false positives, and needs no long-running watcher process. The
trade-off is remembering to run it, which is on the user, not the
system.

## What each script does

**`pregame.ps1`**:
1. `kubectl cordon desktop-j1grrmu` - stop new scheduling there
2. `kubectl drain ... --ignore-daemonsets --delete-emptydir-data` -
   evict existing pods; `api`/`worker` have no `nodeSelector`, so they
   reschedule onto the Pi automatically
3. `wsl.exe -d Ubuntu-24.04 -e sudo systemctl stop k3s-agent` - release
   the kubelet/containerd process itself, not just the pods, so the
   game gets the desktop's full CPU/GPU

**`postgame.ps1`** reverses it: start `k3s-agent`, poll the node until
it reports `Ready` (up to 180s - a cold WSL2 start can take a couple
minutes, not the usual few seconds), then `kubectl uncordon`. Existing
pods don't move back to the desktop automatically once it returns -
Kubernetes doesn't rebalance already-running pods just because a node
became schedulable again - they'll spread across both nodes again on
the next rollout. Expected, not a bug.

## A real bug found by actually running it, not just reading the code

First `postgame.ps1` run failed:

```
error: error executing jsonpath "{.status.conditions[?(@.type==Ready)].status}":
Error executing template: unrecognized identifier Ready.
```

PowerShell mangles double quotes inside a single-quoted string when the
whole thing gets passed through to a *native* executable (`kubectl`) -
`'{...[?(@.type=="Ready")]...}'` arrived at kubectl as
`{...[?(@.type==Ready)]...}`, and JSONPath then rejected the unquoted
`Ready` as a bad identifier instead of a string literal. This is a
PowerShell-calling-native-argv quoting issue, not a kubectl or JSONPath
problem - confirmed by fixing it a different way rather than fighting
escape sequences: switched to `kubectl get node -o json | ConvertFrom-Json`
and filtering the conditions array in PowerShell itself, which sidesteps
native-argv quoting entirely and is more idiomatic PowerShell besides.

## Verified with a real round trip against the live cluster

Not just "the script runs without error" - checked actual cluster state
at each step:

- After `pregame.ps1`: `kubectl get nodes` showed
  `desktop-j1grrmu   NotReady,SchedulingDisabled`; all `backend` pods
  confirmed running on `joe` only; `api.home/health` still returned
  `200` throughout - zero disruption to the live service.
- After `postgame.ps1` (post-fix): node back to plain `Ready`, `kubectl
  uncordon` succeeded, and all 6 Argo CD Applications confirmed
  `Synced`/`Healthy` afterward.

## Usage

```powershell
cd gaming-mode
.\pregame.ps1     # before launching a game
# ... play ...
.\postgame.ps1    # after closing it
```

Or double-click **"Gaming Mode - ON"** / **"Gaming Mode - OFF"** on the
desktop - shortcuts that run the scripts with a visible window (so
drain/wait progress and any warnings are readable) and a "Press Enter
to close" pause at the end rather than flashing shut immediately. Not
committed to the repo (they're a Windows-user-specific `.lnk`, not
portable infrastructure); recreate with:

```powershell
$desktop = [Environment]::GetFolderPath("Desktop")
$WshShell = New-Object -ComObject WScript.Shell
foreach ($pair in @(
    @{Name="Gaming Mode - ON"; Script="pregame.ps1"},
    @{Name="Gaming Mode - OFF"; Script="postgame.ps1"}
)) {
    $s = $WshShell.CreateShortcut("$desktop\$($pair.Name).lnk")
    $s.TargetPath = "powershell.exe"
    $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"D:\homelab\gaming-mode\$($pair.Script)`""
    $s.WorkingDirectory = "D:\homelab\gaming-mode"
    $s.Save()
}
```

Both scripts wrap their logic in `try`/`finally` specifically so the
pause always runs - a failure path that skipped it (e.g. the drain
warning) would flash-close before it could be read.
