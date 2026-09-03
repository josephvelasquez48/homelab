"""Triggers the existing gaming-mode/*.ps1 scripts on the desktop over
SSH, rather than reimplementing their logic here - the dashboard is a
thin remote-trigger wrapper around the already-tested scripts (see
docs/gaming-mode.md), not a second copy of the cordon/drain/uncordon
logic. Two implementations of the same operation is how they quietly
drift apart.
"""
import asyncio

from app.config import (
    GAMING_SCRIPT_DIR,
    GAMING_SSH_HOST,
    GAMING_SSH_KEY_PATH,
    GAMING_SSH_KNOWN_HOSTS_PATH,
    GAMING_SSH_USER,
)


async def run_gaming_script(script_name: str, timeout: float) -> dict:
    script_path = f"{GAMING_SCRIPT_DIR}\\{script_name}"
    remote_command = (
        f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}" -NonInteractive'
    )
    ssh_command = [
        "ssh",
        "-i", GAMING_SSH_KEY_PATH,
        "-o", f"UserKnownHostsFile={GAMING_SSH_KNOWN_HOSTS_PATH}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{GAMING_SSH_USER}@{GAMING_SSH_HOST}",
        remote_command,
    ]

    proc = await asyncio.create_subprocess_exec(
        *ssh_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "success": False,
            "output": f"Timed out after {timeout}s waiting for {script_name} to finish.",
        }

    return {
        "success": proc.returncode == 0,
        "output": stdout.decode(errors="replace") + stderr.decode(errors="replace"),
        "exit_code": proc.returncode,
    }
