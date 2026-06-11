# _download_engine.ps1 — скачивает llama.cpp android-arm64 и распаковывает
# Поддерживает оба формата:
#   - старый: *-android-aarch64.zip  (ggerganov/llama.cpp)
#   - новый:  *-android-arm64.tar.gz (ggml-org/llama.cpp, с ~b9500+)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TargetDir = Join-Path $ScriptDir 'engine\arm64-v8a'
$TempDir   = Join-Path $env:TEMP "llama_android_$(Get-Random)"

New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

# === 1. Определить URL последнего android-arm64 архива ===
Write-Host '[1/4] Запрашиваем последний релиз llama.cpp...'

$url = $null
$archiveName = $null
$tag = $null

# --- Попытка 1: GitHub API ---
try {
    $headers = @{ 'User-Agent' = 'llama-dl/1.0'; 'Accept' = 'application/vnd.github+json' }
    $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/ggml-org/llama.cpp/releases/latest' -Headers $headers -TimeoutSec 30
    $tag = $release.tag_name

    # Ищем android-arm64 или android-aarch64 (zip или tar.gz)
    $asset = $release.assets | Where-Object {
        $_.name -match 'android.*(arm64|aarch64)\.(zip|tar\.gz)$'
    } | Select-Object -First 1

    if ($asset) {
        $url = $asset.browser_download_url
        $archiveName = $asset.name
    }
} catch {
    Write-Host "    API: $_"
}

# --- Попытка 2: HTML-парсинг (без API-лимитов) ---
if (-not $url) {
    Write-Host '    Пробуем через HTML...'
    $ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

    # Получить тег из /releases/latest (редирект)
    $wr = Invoke-WebRequest -Uri 'https://github.com/ggml-org/llama.cpp/releases/latest' -UseBasicParsing -UserAgent $ua -TimeoutSec 30
    $finalUrl = $wr.BaseResponse.ResponseUri.AbsoluteUri
    $tag = $finalUrl.Split('/')[-1]

    if (-not $tag) {
        $m0 = [regex]::Match($wr.Content, '/releases/tag/([\w.\-]+)')
        $tag = $m0.Groups[1].Value
    }
    if (-not $tag) { throw 'Не удалось определить тег релиза' }

    # Получить список файлов из expanded_assets
    $wr2 = Invoke-WebRequest -Uri "https://github.com/ggml-org/llama.cpp/releases/expanded_assets/$tag" -UseBasicParsing -UserAgent $ua

    # Ищем ссылку на android arm64/aarch64 архив
    $m = [regex]::Match($wr2.Content, 'href="(/ggml-org/llama\.cpp/releases/download/[^"]*android[^"]*(?:arm64|aarch64)[^"]*\.(?:zip|tar\.gz))"')
    if (-not $m.Success) {
        # Фолбэк: старый репозиторий ggerganov
        $m = [regex]::Match($wr2.Content, 'href="(/ggerganov/llama\.cpp/releases/download/[^"]*android[^"]*(?:arm64|aarch64)[^"]*\.(?:zip|tar\.gz))"')
    }
    if (-not $m.Success) {
        throw "android-arm64 архив не найден на странице релизов $tag"
    }
    $url = 'https://github.com' + $m.Groups[1].Value
    $archiveName = $url.Split('/')[-1]
}

Write-Host "    Tag : $tag"
Write-Host "    File: $archiveName"

# === 2. Скачать архив ===
Write-Host ''
Write-Host '[2/4] Скачиваем архив...'
$archivePath = Join-Path $TempDir $archiveName
Invoke-WebRequest -Uri $url -OutFile $archivePath -UseBasicParsing -Headers @{ 'User-Agent' = 'curl/7.68.0' }

$sizeMB = [math]::Round((Get-Item $archivePath).Length / 1MB, 1)
Write-Host "    Скачано: $sizeMB МБ"

# === 3. Распаковать ===
Write-Host ''
Write-Host '[3/4] Распаковываем...'
$unzipDir = Join-Path $TempDir 'extracted'
New-Item -ItemType Directory -Path $unzipDir -Force | Out-Null

if ($archiveName -match '\.tar\.gz$') {
    # tar.gz: двухэтапная распаковка
    # Сначала gzip -> tar, потом tar -> files
    # PowerShell 5 не умеет tar.gz нативно, но tar.exe есть в Windows 10 1803+
    $tarExe = Get-Command tar -ErrorAction SilentlyContinue
    if ($tarExe) {
        & tar -xzf $archivePath -C $unzipDir 2>&1 | Out-Null
    } else {
        # Фолбэк: через .NET GZipStream + Expand-Archive
        $tarPath = Join-Path $TempDir ($archiveName -replace '\.gz$', '')
        $inStream = [System.IO.File]::OpenRead($archivePath)
        $gzStream = New-Object System.IO.Compression.GZipStream($inStream, [System.IO.Compression.CompressionMode]::Decompress)
        $outStream = [System.IO.File]::Create($tarPath)
        $gzStream.CopyTo($outStream)
        $outStream.Close(); $gzStream.Close(); $inStream.Close()
        # Распаковать tar
        $tarExe2 = Get-Command tar -ErrorAction SilentlyContinue
        if ($tarExe2) {
            & tar -xf $tarPath -C $unzipDir 2>&1 | Out-Null
        } else {
            throw 'tar.exe не найден. Установите Windows 10 1803+ или распакуйте вручную.'
        }
    }
} else {
    # zip
    Expand-Archive -Path $archivePath -DestinationPath $unzipDir -Force
}

# === 4. Копировать файлы ===
Write-Host ''
Write-Host '[4/4] Копируем файлы...'

if (Test-Path $TargetDir) {
    Write-Host '    Очистка старых файлов...'
    Remove-Item -Recurse -Force $TargetDir
}
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null

# Найти llama-server (ELF-бинарник без расширения или с расширением)
$serverSrc = $null

# Точное имя без расширения
$candidates = Get-ChildItem -Path $unzipDir -Recurse -File | Where-Object { $_.Name -eq 'llama-server' }
if ($candidates) { $serverSrc = $candidates | Select-Object -First 1 }

# С расширением, но не .so
if (-not $serverSrc) {
    $candidates = Get-ChildItem -Path $unzipDir -Recurse -File | Where-Object {
        $_.Name -like 'llama-server.*' -and $_.Extension -ne '.so'
    }
    if ($candidates) { $serverSrc = $candidates | Select-Object -First 1 }
}

# Внутри bin/ подпапки (новый формат tar.gz)
if (-not $serverSrc) {
    $candidates = Get-ChildItem -Path $unzipDir -Recurse -File | Where-Object {
        $_.Name -match '^llama-server' -and $_.Extension -ne '.so'
    }
    if ($candidates) { $serverSrc = $candidates | Select-Object -First 1 }
}

if ($serverSrc) {
    Copy-Item -Path $serverSrc.FullName -Destination (Join-Path $TargetDir 'libllama-server.so') -Force
    Write-Host "    [OK] $($serverSrc.Name)  ->  libllama-server.so"
} else {
    Write-Host '    [ОШИБКА] llama-server не найден в архиве!'
    # Показать что есть
    Write-Host '    Содержимое архива:'
    Get-ChildItem -Path $unzipDir -Recurse -File | ForEach-Object { Write-Host "      $($_.FullName.Replace($unzipDir, '.'))" }
    Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
    exit 1
}

# Копировать все .so
$soFiles = Get-ChildItem -Path $unzipDir -Recurse -Filter '*.so' -File
$soCount = 0
foreach ($f in $soFiles) {
    Copy-Item -Path $f.FullName -Destination (Join-Path $TargetDir $f.Name) -Force
    Write-Host "    [OK] $($f.Name)"
    $soCount++
}

# Также копировать файлы lib*.so* (симлинки или версионированные)
$libFiles = Get-ChildItem -Path $unzipDir -Recurse -File | Where-Object {
    $_.Name -like 'lib*' -and $_.Name -notlike '*.so' -and -not (Test-Path (Join-Path $TargetDir $_.Name))
}
foreach ($f in $libFiles) {
    # Некоторые могут быть .so.1 — переименуем в .so
    $targetName = $f.Name
    if ($targetName -match '\.so\.\d') {
        $targetName = $targetName -replace '\.so\..*$', '.so'
    }
    if (-not (Test-Path (Join-Path $TargetDir $targetName))) {
        Copy-Item -Path $f.FullName -Destination (Join-Path $TargetDir $targetName) -Force
        Write-Host "    [OK] $($f.Name) -> $targetName"
        $soCount++
    }
}

Write-Host "    Итого: $soCount библиотек + llama-server"

# Сохранить тег
$tag | Out-File -Encoding ascii (Join-Path $TargetDir '.version')

# Очистка
Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue

Write-Host ''
Write-Host "Движок $tag установлен в $TargetDir"
exit 0
