$ProgressPreference = 'SilentlyContinue'
$ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
$wr = Invoke-WebRequest -Uri 'https://github.com/ggml-org/llama.cpp/releases/latest' -UseBasicParsing -UserAgent $ua
$tag = $wr.BaseResponse.ResponseUri.AbsoluteUri.Split('/')[-1]
Write-Host "Tag: $tag"

$wr2 = Invoke-WebRequest -Uri "https://github.com/ggml-org/llama.cpp/releases/expanded_assets/$tag" -UseBasicParsing -UserAgent $ua

# Find all Windows assets
$ms = [regex]::Matches($wr2.Content, 'href="(/[^"]*win[^"]*\.(zip|tar\.gz))"')
Write-Host "`nWindows assets ($($ms.Count)):"
foreach ($m in $ms) {
    $name = $m.Groups[1].Value.Split('/')[-1]
    Write-Host "  $name"
}

# Check specific patterns from setup.bat/Portable.bat
$patterns = @('win-vulkan-x64', 'win-cuda-cu12', 'win-cuda-cu11', 'win-cpu-x64')
Write-Host "`nPattern matching:"
foreach ($p in $patterns) {
    $found = $ms | Where-Object { $_.Groups[1].Value -match $p }
    if ($found) {
        $fname = $found[0].Groups[1].Value.Split('/')[-1]
        $isZip = $fname -match '\.zip$'
        Write-Host "  $p => $fname (zip=$isZip)"
    } else {
        Write-Host "  $p => NOT FOUND!"
    }
}
