# Push CLP Tool — 打包脚本
# 用法: powershell -ExecutionPolicy Bypass -File build.ps1
# 输出: dist\PushCLP\ 文件夹，直接复制到 Desktop 即可使用

Set-Location $PSScriptRoot

$OutDir = "dist\PushCLP"

# === [1/4] PyInstaller 打包 ===
Write-Host "=== [1/4] PyInstaller 打包 ===" -ForegroundColor Cyan
python -m PyInstaller PushCLP.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: PyInstaller 失败" -ForegroundColor Red; exit 1
}

# === [2/4] 创建输出文件夹 ===
Write-Host "=== [2/4] 创建输出文件夹 ===" -ForegroundColor Cyan
if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$OutDir\data" | Out-Null

# === [3/4] 复制文件 ===
Write-Host "=== [3/4] 复制文件 ===" -ForegroundColor Cyan

# exe
Copy-Item "dist\PushCLP.exe"  "$OutDir\PushCLP.exe"  -Force

# config 和 SKILL
Copy-Item "config.yaml"  "$OutDir\config.yaml"  -Force
Copy-Item "SKILL.md"     "$OutDir\SKILL.md"     -Force

# data 文件（全部复制）
Get-ChildItem -Path "data" -File | ForEach-Object {
    Copy-Item $_.FullName "$OutDir\data\$($_.Name)" -Force
    Write-Host "  复制: data\$($_.Name)"
}

# === [4/4] 完成 ===
Write-Host "=== [4/4] 完成 ===" -ForegroundColor Green
$exe  = Get-Item "$OutDir\PushCLP.exe"
$size = [math]::Round($exe.Length/1MB, 1)
Write-Host ""
Write-Host "✅ 已生成 $OutDir" -ForegroundColor Green
Write-Host "   PushCLP.exe    $size MB"
Write-Host "   config.yaml"
Write-Host "   SKILL.md"
Get-ChildItem -Path "$OutDir\data" -File | ForEach-Object {
    Write-Host "   data\$($_.Name)"
}
Write-Host ""
Write-Host "同事安装步骤：将 PushCLP 整个文件夹复制到 Desktop" -ForegroundColor Yellow
