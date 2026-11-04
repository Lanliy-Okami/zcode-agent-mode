# PowerShell helper: resolve a REAL CPython interpreter and print its path.
# Resolution order: AGENT_MODE_PYTHON env -> py launcher -> PATH python.exe
# (excluding the broken WindowsApps Store stub) -> well-known install paths.
# The result is cached in %TEMP% (invalidated when the interpreter disappears),
# so per-hook-call cost stays low.
$cacheFile = Join-Path $env:TEMP "agent-mode-python.txt"
if (Test-Path $cacheFile) {
    $cached = (Get-Content $cacheFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($cached -and (Test-Path $cached)) {
        try {
            $output = & $cached -c "import sys, json, asyncio, urllib.request; print('ok')" 2>$null
            if ($output -match 'ok') { Write-Output $cached; exit 0 }
        } catch {}
    }
}

$candidates = @()
if ($env:AGENT_MODE_PYTHON) { $candidates += $env:AGENT_MODE_PYTHON }
try {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $launcherDir = Split-Path $py.Source
        $candidate = Join-Path $launcherDir "python.exe"
        if (Test-Path $candidate) { $candidates += $candidate }
    }
} catch {}
try {
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source -notlike "*WindowsApps*") { $candidates += $onPath.Source }
} catch {}
$localRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
$progRoot = Join-Path $env:ProgramFiles "Python"
foreach ($root in @($localRoot, $progRoot)) {
    if (Test-Path $root) {
        Get-ChildItem $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $candidate = Join-Path $_.FullName "python.exe"
            if (Test-Path $candidate) { $candidates += $candidate }
        }
    }
}
$candidates += "python3.exe"
$candidates += "python.exe"
foreach ($candidate in $candidates) {
    if (-not $candidate) { continue }
    $resolved = $candidate
    if (-not (Test-Path $resolved)) {
        $found = Get-Command $resolved -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        $resolved = $found.Source
    }
    if ($resolved -like "*WindowsApps*") { continue }
    try {
        $output = & $resolved -c "import sys, json, asyncio, urllib.request; print('ok')" 2>$null
        if ($output -match 'ok') {
            Set-Content -Path $cacheFile -Value $resolved -Encoding UTF8
            Write-Output $resolved
            exit 0
        }
    } catch {}
}
exit 1
