$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$env:PYTHONUTF8 = '1'
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -3.14 -m venv venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.14 is required.' }
}
& $python -m pip install -r requirements-local.txt
if ($LASTEXITCODE -ne 0) { throw 'Local dependency installation failed.' }
Push-Location (Join-Path $repoRoot 'local_studio\motion')
try { npm.cmd ci --ignore-scripts --no-audit --no-fund; if ($LASTEXITCODE -ne 0) { throw 'Motion runtime installation failed.' } } finally { Pop-Location }
& $python -m local_studio.bootstrap --model-only
if ($LASTEXITCODE -ne 0) { throw 'Local model installation is incomplete. Start Ollama and retry.' }
& $python -m local_studio --doctor
exit $LASTEXITCODE
