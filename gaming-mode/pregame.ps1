<#
.SYNOPSIS
    Frees the desktop's CPU/GPU for gaming by removing it from the K3s
    cluster: cordon (stop new scheduling) -> drain (evict and reschedule
    existing pods onto the Pi) -> stop k3s-agent (release the kubelet/
    containerd process itself, not just the pods).

    Run manually before launching a game. See ../docs/gaming-mode.md for
    why this is a manual trigger rather than automatic game detection,
    and postgame.ps1 for the reverse.
#>
$NodeName = "desktop-j1grrmu"
$WslDistro = "Ubuntu-24.04"

try {
    Write-Output "==> Cordoning $NodeName (no new pods will be scheduled here)"
    kubectl cordon $NodeName

    Write-Output "==> Draining $NodeName (evicting pods - api/worker will reschedule onto the Pi)"
    kubectl drain $NodeName --ignore-daemonsets --delete-emptydir-data --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "drain did not complete cleanly - check 'kubectl get pods -A -o wide' before proceeding."
        Write-Warning "Not stopping k3s-agent automatically; re-run this script once drain succeeds, or investigate first."
        return
    }

    Write-Output "==> Stopping k3s-agent in WSL2 ($WslDistro)"
    wsl.exe -d $WslDistro -e sudo systemctl stop k3s-agent

    Write-Output "==> Done. Desktop is off the cluster - GPU/CPU fully free for the game."
    Write-Output "    Run postgame.ps1 when you're done to bring it back."
}
finally {
    Read-Host "Press Enter to close"
}
