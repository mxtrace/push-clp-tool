# Push CLP Tool — 打包脚本
# 用法: 在 Push_CLP_Tool 目录下执行: powershell -ExecutionPolicy Bypass -File build.ps1

Set-Location $PSScriptRoot

Write-Host "=== [1/2] PyInstaller 打包 ===" -ForegroundColor Cyan
python -m PyInstaller --onefile --distpath dist --workpath build --specpath . --name PushCLP main.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: PyInstaller 失败，退出码 $LASTEXITCODE" -ForegroundColor Red
    exit 1
}

Write-Host "=== [2/2] 完成 ===" -ForegroundColor Cyan
$exe = Get-Item "dist\PushCLP.exe"
Write-Host ""
Write-Host "=== 打包完成 ===" -ForegroundColor Green
Write-Host "  dist\PushCLP.exe   $([math]::Round($exe.Length/1MB,1)) MB   $($exe.LastWriteTime.ToString('yyyy-MM-dd HH:mm'))"
