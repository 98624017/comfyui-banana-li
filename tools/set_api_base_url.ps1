$ErrorActionPreference = "Stop"

# ================================================
# 固定线路：三条显性线路 + 一条隐藏线路（仅输出混淆值）
# ================================================

$EncodeKeyParts = @(3, 4)
$EncodeKey = 0
foreach ($p in $EncodeKeyParts) {
  $EncodeKey = $EncodeKey -bxor $p
}

$Routes = @{
  "1" = @{ Name = "香港专线"; Key = "hk"; Url = "https://hk-api.aabao.top" }
  "2" = @{ Name = "直连美区"; Key = "us"; Url = "https://api.aabao.top" }
  "3" = @{ Name = "CF专线"; Key = "cf"; Url = "https://cf-api.aabao.top" }
}
$HiddenRoute = @{ Name = "隐藏线路"; Key = "hidden"; Url = "https://api666.zeabur.app" }

function Encode-ApiBaseUrl {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Url
  )

  $bytes = [System.Text.Encoding]::UTF8.GetBytes($Url)
  for ($i = 0; $i -lt $bytes.Length; $i++) {
    $bytes[$i] = $bytes[$i] -bxor $EncodeKey
  }
  return [Convert]::ToBase64String($bytes)
}

function Update-DefaultBaseUrl {
  param(
    [Parameter(Mandatory = $true)][string]$ModuleDir,
    [Parameter(Mandatory = $true)][string]$RouteKey,
    [Parameter(Mandatory = $true)][string]$Url
  )

  $pythonPath = Join-Path $ModuleDir "config_manager.py"
  if (-not (Test-Path $pythonPath)) {
    Write-Host "未找到 config_manager.py，已跳过更新。" -ForegroundColor Red
    return
  }

  $text = Get-Content -Path $pythonPath -Raw -Encoding UTF8

  $codes = $Url.ToCharArray() | ForEach-Object { [int][char]$_ }
  $codesString = ($codes -join ", ")

  $patternCodes = '(?s)_DEFAULT_API_BASE_URL_CODEPOINTS\s*=\s*\[.*?\]'
  $replacementCodes = "_DEFAULT_API_BASE_URL_CODEPOINTS = [$codesString]"
  $text = [System.Text.RegularExpressions.Regex]::Replace($text, $patternCodes, $replacementCodes, 1)

  $patternRouteKey = '_DEFAULT_ROUTE_KEY\s*=\s*".*?"'
  $replacementRouteKey = "_DEFAULT_ROUTE_KEY = `"$RouteKey`""
  $text = [System.Text.RegularExpressions.Regex]::Replace($text, $patternRouteKey, $replacementRouteKey, 1)

  Set-Content -Path $pythonPath -Value $text -Encoding UTF8
  Write-Host "已更新默认线路为 $RouteKey ($Url)" -ForegroundColor Green
}

# ========== 交互：选择默认线路 ==========
Write-Host "请选择默认显性线路（仅三选一）：" -ForegroundColor Cyan
foreach ($entry in $Routes.GetEnumerator() | Sort-Object Key) {
  Write-Host "  $($entry.Key)) $($entry.Value.Name) [$($entry.Value.Url)]"
}
$choice = Read-Host "请输入 1 / 2 / 3"
if (-not $Routes.ContainsKey($choice)) {
  Write-Host "无效选择，操作已取消。" -ForegroundColor Yellow
  exit 1
}
$selected = $Routes[$choice]

# ========== 输出混淆值（含隐藏线路） ==========
Write-Host "`n混淆后的 Base URL（XOR+Base64）：" -ForegroundColor Cyan
foreach ($route in ($Routes.Values + $HiddenRoute)) {
  $encoded = Encode-ApiBaseUrl -Url $route.Url
  Write-Host ("  {0} ({1}): {2}" -f $route.Name, $route.Key, $encoded)
}

# ========== 更新 config_manager.py 默认值 ==========
$moduleDir = Split-Path -Path $PSScriptRoot -Parent
try {
  Update-DefaultBaseUrl -ModuleDir $moduleDir -RouteKey $selected.Key -Url $selected.Url
  Write-Host "完成。重启 ComfyUI 后生效。" -ForegroundColor Green
} catch {
  Write-Host "更新失败: $_" -ForegroundColor Red
  exit 1
}

