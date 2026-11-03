# 启动 ZCode 官方通道桥（Windows PowerShell）
param(
  [int]$Port = 8899,
  [int]$Concurrency = 2,
  [int]$TimeoutSec = 600
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = (Resolve-Path (Join-Path $here "..\..\..")).Path
$python = Join-Path $project ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "找不到 $python" }
$log = Join-Path $project "tmp\zcode_bridge.log"
New-Item -ItemType Directory -Force -Path (Join-Path $project "tmp") | Out-Null
if (Test-Path $log) { Move-Item $log "$log.1" -Force }
Start-Process -FilePath $python -ArgumentList @(
  (Join-Path $here "bridge.py"),
  "--port", "$Port",
  "--concurrency", "$Concurrency",
  "--timeout", "$TimeoutSec"
) -WorkingDirectory $project -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Milliseconds 500
  try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:$Port/health") -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) {
      Write-Output ("bridge up: http://127.0.0.1:$Port/v1  log=" + $log)
      exit 0
    }
  } catch {}
}
Write-Output "bridge 启动失败，查看 $log / $log.err"
exit 1
