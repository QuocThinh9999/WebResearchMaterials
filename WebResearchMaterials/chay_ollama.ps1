$ErrorActionPreference = "Stop"

$thuMucDuAn = Split-Path -Parent $MyInvocation.MyCommand.Path
$tepOllama = Join-Path $thuMucDuAn ".ollama-runtime\ollama.exe"
$thuMucMoHinh = Join-Path $thuMucDuAn ".ollama-models"
$thuMucNhatKy = Join-Path $thuMucDuAn ".ollama-logs"

if (-not (Test-Path -LiteralPath $tepOllama)) {
    throw "Chưa có Ollama trong dự án. Hãy chạy .\cai_dat_ollama.ps1 trước."
}

New-Item -ItemType Directory -Force -Path $thuMucMoHinh, $thuMucNhatKy | Out-Null
$env:OLLAMA_MODELS = $thuMucMoHinh
$env:OLLAMA_HOST = "127.0.0.1:11434"

try {
    Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null
    Write-Host "Ollama đang chạy tại http://127.0.0.1:11434"
    exit 0
} catch {
    # Khởi động máy chủ local ở chế độ nền.
}

$tepRa = Join-Path $thuMucNhatKy "ollama.out.log"
$tepLoi = Join-Path $thuMucNhatKy "ollama.error.log"
Start-Process -FilePath $tepOllama -ArgumentList "serve" -WindowStyle Hidden `
    -RedirectStandardOutput $tepRa -RedirectStandardError $tepLoi | Out-Null

for ($lan = 1; $lan -le 30; $lan++) {
    Start-Sleep -Milliseconds 500
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null
        Write-Host "Đã khởi động Ollama tại http://127.0.0.1:11434"
        exit 0
    } catch {
        # Chờ máy chủ sẵn sàng.
    }
}

throw "Không thể khởi động Ollama. Xem nhật ký trong thư mục .ollama-logs."
