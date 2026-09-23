# Install the desktop ring agent for the current Windows user.
#
#   powershell -ExecutionPolicy Bypass -File apps\phone\agent\install-agent.ps1 -Token <PHONE_AGENT_TOKEN>
#
# Writes %APPDATA%\phone-bridge\agent.json, adds a Startup-folder shortcut
# that runs the agent with pythonw (no console window) at login, and
# starts it now. Re-running updates the token and restarts the agent.
param(
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$Url = "https://phone.home:8443"
)
$ErrorActionPreference = "Stop"

$agent = Join-Path $PSScriptRoot "phone_agent.pyw"
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $python = (Get-Command python.exe).Source
    $pythonw = Join-Path (Split-Path $python) "pythonw.exe"
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to python.exe" }

$configDir = Join-Path $env:APPDATA "phone-bridge"
New-Item -ItemType Directory -Force $configDir | Out-Null
@{ url = $Url; token = $Token } | ConvertTo-Json | Out-File -Encoding ascii (Join-Path $configDir "agent.json")

$startup = [Environment]::GetFolderPath("Startup")
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $startup "Phone ring agent.lnk"))
$link.TargetPath = $pythonw
$link.Arguments = "`"$agent`""
$link.WorkingDirectory = $PSScriptRoot
$link.Description = "Pops up incoming iPhone calls from the Pi phone bridge"
$link.Save()

# Desktop shortcut to the phone page, in Firefox (the browser the ring
# agent's Answer button opens). GetFolderPath follows OneDrive redirection.
$firefox = @(
    (Join-Path $env:ProgramFiles "Mozilla Firefox\firefox.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Mozilla Firefox\firefox.exe")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$desktop = [Environment]::GetFolderPath("Desktop")
$page = $shell.CreateShortcut((Join-Path $desktop "Phone.lnk"))
if ($firefox) {
    $page.TargetPath = $firefox
    $page.Arguments = "--new-window $Url"
    $page.IconLocation = "$firefox,0"
} else {
    # No Firefox: hand the URL to the default browser.
    $page.TargetPath = Join-Path $env:WINDIR "explorer.exe"
    $page.Arguments = $Url
}
$page.Description = "iPhone calls on this PC"
$page.Save()

# Restart: stop any agent already running from this script path.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*phone_agent.pyw*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process -FilePath $pythonw -ArgumentList "`"$agent`"" -WorkingDirectory $PSScriptRoot

Write-Output "Ring agent installed and running. Log: $configDir\agent.log"
