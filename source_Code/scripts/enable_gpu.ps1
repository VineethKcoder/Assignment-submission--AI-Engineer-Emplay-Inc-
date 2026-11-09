# Stop Streamlit before running: loaded PyTorch DLLs cannot be replaced on Windows.
# Official matching Windows wheels: https://pytorch.org/get-started/previous-versions/
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$pythonPath = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Virtual environment missing. Run scripts\setup_windows.ps1 first."
}

Write-Host "Installing CUDA 12.8 PyTorch and matching torchvision. Stop Streamlit first."
& $pythonPath -m pip install --upgrade "torch==2.11.0" "torchvision==0.26.0" --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) {
    throw "CUDA wheel installation failed. Check internet access and stop processes using this virtual environment, then rerun."
}

# Verify real GPU execution before changing the application's device setting.
$verifyGpu = @'
import core
import torch
import torchvision
from pathlib import Path
from dotenv import set_key

if not torch.cuda.is_available():
    raise SystemExit('CUDA is unavailable after installation. Check the NVIDIA driver and restart Windows if needed.')
x = torch.ones((64, 64), device='cuda')
y = x @ x
torch.cuda.synchronize()
assert y.device.type == 'cuda' and y[0, 0].item() == 64
set_key(str(Path('.env')), 'DEVICE', 'cuda', quote_mode='never')
print('PyTorch:', torch.__version__, '| torchvision:', torchvision.__version__)
print('CUDA runtime:', torch.version.cuda)
print('GPU:', torch.cuda.get_device_name(0))
print('GPU calculation passed. DEVICE=cuda saved to .env.')
'@
& $pythonPath -c $verifyGpu
if ($LASTEXITCODE -ne 0) {
    throw "GPU verification failed; the device setting was not changed."
}
Write-Host "Restart the UI: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_ui.ps1 -Port 8502" -ForegroundColor Green
