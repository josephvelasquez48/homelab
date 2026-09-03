# Gaming mode

Not part of the original 18-step roadmap - added afterward. The desktop
is both the K3s worker node and the gaming rig, so launching a game
competes with `api`/`worker` pods for CPU, and with Ollama for GPU.
`gaming-mode/pregame.ps1` and `gaming-mode/postgame.ps1` cleanly remove
the desktop from the cluster before gaming and bring it back after.

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
