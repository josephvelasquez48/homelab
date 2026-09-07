<#
.SYNOPSIS
  Re-registers the WSL2 kernel WireGuard socket with the Windows host so
  flannel's cross-node tunnel can receive inbound packets after a reboot.

.DESCRIPTION
  Mirrored-mode WSL2 does not reliably register a *kernel-owned* UDP
  socket (as opposed to a normal userspace bind) for Windows-side inbound
  visibility. flannel's wireguard-native backend owns UDP 51820 from
  inside the kernel, so after a reboot the Pi's handshake responses reach
  the Windows host and are then dropped before WSL sees them - with the
  firewall rules on both ends completely correct, which is what makes it
  so misleading. `wg show` sits at "0 B received" while happily sending.

  A userspace bind attempt against the same port - one that FAILS with
  "Address already in use" - is enough to make WSL2 notice and register
  it. See docs/kubernetes.md, "Fix, part 2", where this was first found.

  Intended to run once at logon via Task Scheduler. Safe to run by hand
  at any time, and safe to run when nothing is wrong: if the tunnel is
  already handshaking it exits immediately without touching anything.

.NOTES
  The refusal to bind when the port is FREE is the important safety
  property here, not an edge case. If flannel has not claimed 51820 yet,
  a successful bind would mean this script is holding the port that
  WireGuard is about to need - turning a boot-time fix into a boot-time
  outage. Better to do nothing and let the next run handle it.
#>
[CmdletBinding()]
param(
    [string] $Distro = 'Ubuntu-24.04',
    [int]    $WaitSeconds = 180,
    [int]    $Attempts = 3
)

$ErrorActionPreference = 'Stop'

function Invoke-Wsl {
    param([string] $Script)
    # -u root: reading wg state and binding a privileged port both need it.
    wsl.exe -d $Distro -u root -- bash -lc $Script 2>&1
}

function Get-HandshakeAge {
    # Epoch seconds of the last completed handshake; 0 means "never".
    #
    # Parsed here rather than with awk inside the WSL command: quoting an
    # awk program through PowerShell -> wsl.exe -> bash mangles it into a
    # no-op that returns the whole line, which then fails to parse as an
    # int and silently reports "never" for a perfectly healthy tunnel.
    $raw = Invoke-Wsl 'wg show flannel-wg latest-handshakes'
    foreach ($line in @($raw)) {
        $fields = ([string] $line).Trim() -split '\s+'
        $value = 0
        if ($fields.Count -ge 2 -and [int]::TryParse($fields[-1], [ref] $value)) { return $value }
    }
    return 0
}

Write-Output "[wg-register] waiting for flannel-wg in $Distro (up to ${WaitSeconds}s)"

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$ready = $false
while ((Get-Date) -lt $deadline) {
    $iface = Invoke-Wsl 'ip link show flannel-wg >/dev/null 2>&1 && echo present || echo absent'
    if ($iface -match 'present') { $ready = $true; break }
    Start-Sleep -Seconds 5
}

if (-not $ready) {
    Write-Output "[wg-register] flannel-wg never appeared - k3s-agent may not be running. Nothing to do."
    exit 0
}

if ((Get-HandshakeAge) -gt 0) {
    Write-Output "[wg-register] tunnel is already handshaking. Nothing to do."
    exit 0
}

for ($i = 1; $i -le $Attempts; $i++) {
    # Only ever poke a port that something else already owns. A free port
    # means flannel has not bound yet, and binding it ourselves would
    # cause the very outage this script exists to prevent.
    $sockets = Invoke-Wsl 'ss -uln'
    if (-not (@($sockets) -match ':51820')) {
        Write-Output "[wg-register] attempt ${i}: 51820 is not bound yet - skipping the bind attempt on purpose"
        Start-Sleep -Seconds 10
        continue
    }

    $result = Invoke-Wsl 'timeout 5 nc -u -l 51820 2>&1 | head -1'
    Write-Output "[wg-register] attempt ${i}: bind said '$($result -join ' ')' (failure is the expected, working outcome)"

    Start-Sleep -Seconds 15
    $age = Get-HandshakeAge
    if ($age -gt 0) {
        Write-Output "[wg-register] handshake completed - cross-node networking is up."
        exit 0
    }
}

Write-Output "[wg-register] still no handshake after $Attempts attempts."
Write-Output "[wg-register] Check the classic firewall rule and both nodes' 10.42 routes - see docs/kubernetes.md."
exit 1
