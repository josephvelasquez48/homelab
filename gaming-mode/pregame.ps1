<#
.SYNOPSIS
    Frees the desktop's CPU/GPU for gaming by removing it from the K3s
    cluster: cordon (stop new scheduling) -> drain (evict and reschedule
    existing pods onto the Pi) -> stop k3s-agent (release the kubelet/
    containerd process itself, not just the pods).

    Run manually before launching a game, via the desktop shortcut, or
    remotely over SSH from the Pi dashboard (-NonInteractive). See
    ../docs/gaming-mode.md for why this is a manual trigger rather than
    automatic game detection, and postgame.ps1 for the reverse.

.PARAMETER NonInteractive
    Skip the "Press Enter to close" pause. Required for any non-TTY
    caller (e.g. `ssh host powershell.exe -File pregame.ps1
    -NonInteractive`, which the Pi dashboard uses) - Read-Host on a
    closed/absent stdin either hangs or throws, neither of which a
    remote caller waiting on the SSH command to return can recover from.
#>
param(
    [switch]$NonInteractive
)

$NodeName = "desktop-j1grrmu"
$WslDistro = "Ubuntu-24.04"
$exitCode = 0

try {
    Write-Output "==> Cordoning $NodeName (no new pods will be scheduled here)"
    kubectl cordon $NodeName

    Write-Output "==> Draining $NodeName (evicting pods - api/worker will reschedule onto the Pi)"
    kubectl drain $NodeName --ignore-daemonsets --delete-emptydir-data --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "drain did not complete cleanly - check 'kubectl get pods -A -o wide' before proceeding."
        Write-Warning "Not stopping k3s-agent automatically; re-run this script once drain succeeds, or investigate first."
        $exitCode = 1
        return
    }

    Write-Output "==> Stopping k3s-agent in WSL2 ($WslDistro)"
    wsl.exe -d $WslDistro -e sudo systemctl stop k3s-agent

    Write-Output "==> Done. Desktop is off the cluster - GPU/CPU fully free for the game."
    Write-Output "    Run postgame.ps1 when you're done to bring it back."
}
finally {
    if (-not $NonInteractive) {
        Read-Host "Press Enter to close"
    }
    exit $exitCode
}
