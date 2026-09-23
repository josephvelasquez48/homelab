# Install the desktop ring agent for the current Windows user.
#
#   powershell -ExecutionPolicy Bypass -File apps\phone\agent\install-agent.ps1 -Token <PHONE_AGENT_TOKEN>
#
# Creates the agent's own venv (%LOCALAPPDATA%\phone-bridge\agent-venv)
# with pywebview, writes %APPDATA%\phone-bridge\agent.json, adds a
# Startup-folder shortcut that runs the agent with the venv's pythonw (no
# console window) at login, and starts it now. Re-running updates the
# token and pywebview and restarts the agent.
param(
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$Url = "https://phone.home:8443"
)
$ErrorActionPreference = "Stop"

$agent = Join-Path $PSScriptRoot "phone_agent.pyw"

# Its own venv, so pywebview (and pythonnet under it) never touch the
# user's main Python install.
$venv = Join-Path $env:LOCALAPPDATA "phone-bridge\agent-venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    python -m venv $venv
    if ($LASTEXITCODE) { throw "couldn't create $venv" }
}
& (Join-Path $venv "Scripts\python.exe") -m pip install --quiet --disable-pip-version-check --upgrade "pywebview>=6.2" "pystray>=0.19" "pillow>=10"
if ($LASTEXITCODE) { throw "pip install of the agent's packages failed" }
$pythonw = Join-Path $venv "Scripts\pythonw.exe"

$configDir = Join-Path $env:APPDATA "phone-bridge"
New-Item -ItemType Directory -Force $configDir | Out-Null
@{ url = $Url; token = $Token } | ConvertTo-Json | Out-File -Encoding ascii (Join-Path $configDir "agent.json")

$startup = [Environment]::GetFolderPath("Startup")
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $startup "Phone ring agent.lnk"))
$link.TargetPath = $pythonw
$link.Arguments = "`"$agent`""
$link.WorkingDirectory = $PSScriptRoot
$link.Description = "Takes incoming iPhone calls in a popup, via the Pi phone bridge"
$link.Save()

# Desktop shortcut: opens the agent's own phone window (the whole app - no
# browser). If the agent is already running, the new copy just tells it to
# open (--show); if not, it starts and opens. GetFolderPath follows OneDrive
# redirection. The icon is the tray icon, saved as a .ico.
$icon = Join-Path $env:LOCALAPPDATA "phone-bridge\phone.ico"
& (Join-Path $venv "Scripts\python.exe") -c "import sys; sys.path.insert(0, r'$PSScriptRoot'); from tray import icon_image, GREEN; icon_image(GREEN).save(r'$icon', sizes=[(16,16),(32,32),(48,48),(64,64)])"
$desktop = [Environment]::GetFolderPath("Desktop")
$page = $shell.CreateShortcut((Join-Path $desktop "Phone.lnk"))
$page.TargetPath = $pythonw
$page.Arguments = "`"$agent`" --show"
$page.WorkingDirectory = $PSScriptRoot
$page.IconLocation = "$icon,0"
$page.Description = "iPhone calls on this PC"
$page.Save()

# Restart: stop any agent already running, from any Python, and the
# WebView2 processes it leaves behind - they hold the agent's profile, and
# a new agent started while they're alive fails with "The requested
# resource is in use".
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*phone_agent.pyw*" -or ($_.Name -eq "msedgewebview2.exe" -and $_.CommandLine -like "*phone-bridge*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
for ($i = 0; $i -lt 20; $i++) {
    $left = @(Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe'" | Where-Object { $_.CommandLine -like "*phone-bridge*" })
    if ($left.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
}
Start-Process -FilePath $pythonw -ArgumentList "`"$agent`"" -WorkingDirectory $PSScriptRoot

Write-Output "Ring agent installed and running. Log: $configDir\agent.log"
