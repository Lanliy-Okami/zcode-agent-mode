# PowerShell launcher for the agent-mode enforcement hook: resolves a real
# CPython interpreter (never the WindowsApps Store stub), then execs the hook.
# argv[1] of this script is passed through as the hook command.
$ErrorActionPreference = "Stop"
$pluginRoot = $env:AGENT_MODE_PLUGIN_ROOT
if (-not $pluginRoot) { $pluginRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath) }
$hookCommand = $args[0]
if (-not $hookCommand) { exit 0 }
$findScript = Join-Path $pluginRoot "scripts\find_python.ps1"
$python = & $findScript
if (-not $python) { exit 0 }  # fail open: no interpreter, no enforcement
$hook = Join-Path $pluginRoot "scripts\agent_mode_hook.py"
& $python -X utf8 -I $hook $hookCommand
exit $LASTEXITCODE
