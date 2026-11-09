# One-time setup for Windows (GPU). Run from the project root:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
# Add -Cpu to install the CPU-only PyTorch build instead.
param([switch]$Cpu)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment .venv ..."
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
}
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

# Install general dependencies first; then select a matching torch/torchvision
# pair explicitly so an existing CPU build cannot satisfy the GPU install.
& $py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - put your GROQ_API_KEY in it." -ForegroundColor Yellow
}

if ($Cpu) {
    & $py -m pip install --upgrade "torch==2.11.0" "torchvision==0.26.0" --index-url https://download.pytorch.org/whl/cpu
    if ($LASTEXITCODE -ne 0) { throw "CPU PyTorch installation failed." }
    & $py -c "from dotenv import set_key; set_key('.env', 'DEVICE', 'cpu', quote_mode='never')"
    if ($LASTEXITCODE -ne 0) { throw "CPU configuration failed." }
} else {
    & "$PSScriptRoot\enable_gpu.ps1"
}

& $py -c "import torch; print('torch', torch.__version__, '| CUDA available:', torch.cuda.is_available())"
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Installation tests failed." }
Write-Host "`nSetup done. Activate with: .\.venv\Scripts\Activate.ps1" -ForegroundColor Green
Write-Host "Next: python main.py index .\data\Bid1 .\data\Bid2 --force ; python main.py --bid .\data\Bid1 --bid .\data\Bid2"
