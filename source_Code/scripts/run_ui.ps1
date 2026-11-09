# Launch from any directory using this project's virtual environment.
param([int]$Port = 8501)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$pythonPath = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Virtual environment missing. Run scripts\setup_windows.ps1 first."
}
& $pythonPath -m streamlit run ui\app.py --server.address 127.0.0.1 --server.port $Port --server.fileWatcherType none --browser.gatherUsageStats false
exit $LASTEXITCODE
