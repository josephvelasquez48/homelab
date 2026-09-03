<#
.SYNOPSIS
    Reverses pregame.ps1: starts k3s-agent, waits for the node to report
    Ready, then uncordons it so the scheduler considers it again.

    Run manually after closing the game. See ../docs/gaming-mode.md.
#>
$NodeName = "desktop-j1grrmu"
$WslDistro = "Ubuntu-24.04"
$ReadyTimeoutSeconds = 180

try {
    Write-Output "==> Starting k3s-agent in WSL2 ($WslDistro)"
    wsl.exe -d $WslDistro -e sudo systemctl start k3s-agent

    Write-Output "==> Waiting for $NodeName to report Ready (up to ${ReadyTimeoutSeconds}s - a cold WSL2 start can take a couple minutes)"
    $elapsed = 0
    $ready = $false
    while ($elapsed -lt $ReadyTimeoutSeconds) {
        # Using kubectl's own JSON output + ConvertFrom-Json rather than -o
        # jsonpath - PowerShell mangles the double quotes inside a jsonpath
        # filter expression when passing it through to a native executable
        # (found by testing: '@.type=="Ready"' arrives at kubectl as
        # '@.type==Ready', which jsonpath then rejects as a bad identifier).
        $node = kubectl get node $NodeName -o json 2>$null | ConvertFrom-Json
        $readyCondition = $node.status.conditions | Where-Object { $_.type -eq "Ready" }
        if ($readyCondition -and $readyCondition.status -eq "True") {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 5
        $elapsed += 5
        Write-Output "    ...still waiting (${elapsed}s elapsed)"
    }

    if (-not $ready) {
        Write-Warning "Node did not report Ready within ${ReadyTimeoutSeconds}s."
        Write-Warning "Not uncordoning automatically - check 'wsl -d $WslDistro -e sudo systemctl status k3s-agent' and 'kubectl get nodes' before retrying."
        return
    }

    Write-Output "==> $NodeName is Ready. Uncordoning."
    kubectl uncordon $NodeName

    Write-Output "==> Done. Desktop is back in the cluster."
    Write-Output "    Existing pods won't move back automatically (K8s doesn't rebalance already-running pods) -"
    Write-Output "    they'll spread across both nodes again on the next rollout. That's expected, not a problem."
}
finally {
    Read-Host "Press Enter to close"
}
