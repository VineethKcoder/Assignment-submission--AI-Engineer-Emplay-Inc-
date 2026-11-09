# Regenerate every deliverable (index, extraction JSONs, eval report, Q&A log, example trace).
#   powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }

& $py -m pytest -q
& $py main.py index .\data\Bid1 .\data\Bid2
& $py main.py eval
& $py main.py --bid .\data\Bid1 --bid .\data\Bid2
& $py main.py qa-log

$trace = Get-ChildItem outputs\traces\extract-Bid1-*.jsonl | Sort-Object LastWriteTime | Select-Object -Last 1
if ($trace) {
    Copy-Item $trace.FullName outputs\traces\example_trace_Bid1.jsonl -Force
    & $py main.py trace-report outputs\traces\example_trace_Bid1.jsonl -o docs\example_trace.md
}
Write-Host "All deliverables regenerated: outputs\, eval\results.md, docs\example_trace.md" -ForegroundColor Green
