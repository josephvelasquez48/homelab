' Runs the WSL2 keepalive ping (see docs/kubernetes.md, "actually fixing
' the WSL2 idle-timeout flakiness") with zero visible window.
'
' The Scheduled Task originally invoked wsl.exe directly, which - being
' a console application - popped a visible window every single time it
' fired (every minute), which is exactly the kind of thing you notice
' immediately on a desktop you actually use. WScript.Shell.Run's third
' argument (windowStyle 0 = hidden) is the standard, reliable fix for
' "run this with Task Scheduler and never show a window" - more robust
' than cmd.exe /min tricks, which can still flash briefly.
Set objShell = CreateObject("WScript.Shell")
objShell.Run "wsl.exe -d Ubuntu-24.04 -e /bin/true", 0, True
