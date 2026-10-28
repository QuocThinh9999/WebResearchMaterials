$ErrorActionPreference = "Stop"

$thuMucDuAn = Split-Path -Parent $MyInvocation.MyCommand.Path
$thuMucRuntime = Join-Path $thuMucDuAn ".ollama-runtime"
$tepOllama = Join-Path $thuMucRuntime "ollama.exe"
$tepNen = Join-Path ([System.IO.Path]::GetTempPath()) "ollama-windows-amd64.zip"
$diaChiTai = "https://ollama.com/download/ollama-windows-amd64.zip"
$moHinhNgonNgu = "qwen3:4b"
$moHinhEmbedding = "qwen3-embedding:0.6b"
$tepCauHinh = Join-Path $thuMucDuAn ".env"
if (Test-Path -LiteralPath $tepCauHinh) {
    foreach ($dong in Get-Content -LiteralPath $tepCauHinh) {
        if ($dong -match '^\s*OLLAMA_MODEL\s*=\s*(.+?)\s*$') { $moHinhNgonNgu = $Matches[1].Trim('"', "'") }
        if ($dong -match '^\s*OLLAMA_EMBEDDING_MODEL\s*=\s*(.+?)\s*$') { $moHinhEmbedding = $Matches[1].Trim('"', "'") }
    }
}

if (-not (Test-Path -LiteralPath $tepOllama)) {
    Write-Host "Đang tải Ollama bản độc lập từ trang chính thức..."
    & curl.exe --fail --location --continue-at - --output $tepNen $diaChiTai
    if ($LASTEXITCODE -ne 0) { throw "Không thể tải bộ chạy Ollama." }
    New-Item -ItemType Directory -Force -Path $thuMucRuntime | Out-Null
    Expand-Archive -LiteralPath $tepNen -DestinationPath $thuMucRuntime -Force
    Remove-Item -LiteralPath $tepNen -Force
}

& (Join-Path $thuMucDuAn "chay_ollama.ps1")
$env:OLLAMA_MODELS = Join-Path $thuMucDuAn ".ollama-models"
$env:OLLAMA_HOST = "127.0.0.1:11434"

Write-Host "Đang tải mô hình ngôn ngữ $moHinhNgonNgu..."
& $tepOllama pull $moHinhNgonNgu
if ($LASTEXITCODE -ne 0) { throw "Không thể tải $moHinhNgonNgu." }

Write-Host "Đang tải mô hình embedding $moHinhEmbedding..."
& $tepOllama pull $moHinhEmbedding
if ($LASTEXITCODE -ne 0) { throw "Không thể tải $moHinhEmbedding." }

Write-Host "Hoàn tất. Ollama và các mô hình đã sẵn sàng trong thư mục dự án."
