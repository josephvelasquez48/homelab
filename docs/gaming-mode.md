# Gaming mode

Frees the desktop's GPU for a game: **Free the GPU** on the dashboard
(`POST /api/gpu/release`, login required - [dashboard.md](dashboard.md)).
Not on the original roadmap.

## How it works now

Ollama holds ~4.7 GB of the RTX 3070 Ti's 8 GB while a model is loaded. The
button asks Ollama to unload it: `POST /api/generate` with `keep_alive: 0`
and no prompt. Ollama replies `done_reason: unload` and generates nothing
(checked against v0.32.5, not taken from the docs).

**There's no "off" button, on purpose.** The next AI request loads the model
again by itself, so an "off" would do nothing. Ollama also unloads on its
own after sitting idle - this just makes it immediate.

## How it used to work

Until 2026-09-10 the desktop was also the cluster's worker, so a game
competed with pods for CPU as well as GPU. Two PowerShell scripts took the
node out of the cluster and put it back:

- **`pregame.ps1`:** `kubectl cordon` and `drain` the node (pods moved to the
  Pi), then stop `k3s-agent` inside WSL.
- **`postgame.ps1`:** start the agent, wait up to 180 s for `Ready`, uncordon.

The dashboard ran them over SSH. When the worker moved to the Mac
([node-migration.md](node-migration.md)) there was no node left to drain,
so only the GPU half of the problem remained. The scripts, the SSH key and
the remote PowerShell were deleted - the biggest attack surface in the
project ([security-testing.md](security-testing.md), finding 1).

**Why manual, not automatic:** detecting games reliably is hard (other GPU
apps give false positives, borderless windows false negatives). A button
never misfires; the cost is remembering to press it.

## A bug worth remembering

The first `postgame.ps1` run failed with `unrecognized identifier Ready`.
PowerShell strips double quotes from arguments passed to native programs,
so `'{...[?(@.type=="Ready")]...}'` reached `kubectl` as
`{...[?(@.type==Ready)]...}`. The fix avoided the quoting instead of
escaping it: `kubectl get node -o json | ConvertFrom-Json`, then filter in
PowerShell.
