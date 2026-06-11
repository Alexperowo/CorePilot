# _download_llama_win.ps1 — скачивает llama.cpp для Windows (с HTML-фолбэком)
# Параметры: -Match <паттерн> -OutFile <путь>
# Пример:  _download_llama_win.ps1 -Match "win-vulkan-x64" -OutFile "C:\tmp\llama.zip"
param(
    [Parameter(Mandatory=$true)][string]$Match,
    [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$url = $null
$assetName = $null

# --- Попытка 1: GitHub API ---
try {
    $h = @{ 'User-Agent' = 'CorePilot-Setup'; 'Accept' = 'application/vnd.github+json' }
    $r = Invoke-RestMethod -Uri 'https://api.github.com/repos/ggml-org/llama.cpp/releases/latest' -Headers $h -TimeoutSec 30
    $a = $r.assets | Where-Object { $_.name -match $Match -and $_.name -match '\.zip$' } | Select-Object -First 1
    if ($a) {
        $url = $a.browser_download_url
        $assetName = $a.name
    }
} catch {
    Write-Host "  API: $_"
}

# --- Попытка 2: HTML-парсинг (без API-лимитов) ---
if (-not $url) {
    Write-Host '  API недоступен, пробуем через HTML...'
    $ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    $wr = Invoke-WebRequest -Uri 'https://github.com/ggml-org/llama.cpp/releases/latest' -UseBasicParsing -UserAgent $ua -TimeoutSec 30
    $tag = $wr.BaseResponse.ResponseUri.AbsoluteUri.Split('/')[-1]
    if (-not $tag) {
        $m0 = [regex]::Match($wr.Content, '/releases/tag/([\w.\-]+)')
        $tag = $m0.Groups[1].Value
    }
    if (-not $tag) { throw 'Не удалось определить тег релиза' }

    $wr2 = Invoke-WebRequest -Uri "https://github.com/ggml-org/llama.cpp/releases/expanded_assets/$tag" -UseBasicParsing -UserAgent $ua
    $pattern = "href=""(/ggml-org/llama\.cpp/releases/download/[^""]*${Match}[^""]*\.zip)"""
    $m = [regex]::Match($wr2.Content, $pattern)
    if (-not $m.Success) { throw "Ассет $Match не найден на странице релизов $tag" }
    $url = 'https://github.com' + $m.Groups[1].Value
    $assetName = $url.Split('/')[-1]
}

Write-Host "  Найдено: $assetName"
Invoke-WebRequest -Uri $url -OutFile $OutFile -UseBasicParsing -Headers @{ 'User-Agent' = 'curl/7.68.0' }
$sizeMB = [math]::Round((Get-Item $OutFile).Length / 1MB, 1)
Write-Host "  Скачано: $sizeMB МБ"
