# PowerShell launcher for the agent-mode MCP server: resolves a real CPython
# interpreter (never the broken WindowsApps Store stub), then execs the server.
# Extra arguments (e.g. -X utf8) are handled here; .mcp.json appends none.
$ErrorActionPreference = "Stop"
$pluginRoot = $env:AGENT_MODE_PLUGIN_ROOT
if (-not $pluginRoot) { $pluginRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath) }
$findScript = Join-Path $pluginRoot "scripts\find_python.ps1"
$python = & $findScript
if (-not $python) {
    Write-Error "agent-mode: no usable CPython interpreter found (set AGENT_MODE_PYTHON)"
    exit 1
}
$server = Join-Path $pluginRoot "mcp\run_code_server.py"
& $python -X utf8 -I $server @args
exit $LASTEXITCODE
