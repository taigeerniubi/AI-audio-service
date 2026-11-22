# 在本机启动构建后的音频服务与 frpc，任一子进程退出时统一回收并交由任务计划重启。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PublicOrigin,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:PYTHONUTF8 = '1'
$env:NO_PROXY = "$env:NO_PROXY,localhost,127.0.0.1,::1"
$runtimeDir = Join-Path $projectRoot 'output\deployment\runtime'
$logDir = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$frpcExe = Join-Path $projectRoot 'output\deployment\frp\frp_0.72.0_windows_amd64\frpc.exe'
$frpcConfig = Join-Path $runtimeDir 'frpc.toml'
foreach ($requiredFile in @($pythonExe, $frpcExe, $frpcConfig, (Join-Path $projectRoot 'frontend\dist\index.html'))) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) { throw "Missing required file: $requiredFile" }
}
function Write-DeploymentLog([string]$Message) {
    [IO.File]::AppendAllText((Join-Path $logDir 'supervisor.log'), ((Get-Date -Format o) + ' ' + $Message + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
}
trap { Write-DeploymentLog ('ERROR ' + $_.Exception.Message); throw $_ }
Write-DeploymentLog 'Validating gateway configuration'
& $frpcExe verify -c $frpcConfig
if ($LASTEXITCODE -ne 0) { throw 'Invalid frpc configuration' }
$env:PUBLIC_ORIGIN = $PublicOrigin
& $pythonExe -c 'from backend.core.config import Settings; Settings.from_env()'
if ($LASTEXITCODE -ne 0) { throw 'Invalid application configuration' }
Write-Output 'Application settings valid'
Write-DeploymentLog 'Application settings valid'
if ($Check) { exit 0 }

# 先约束监督进程，确保虚拟环境启动器再生成的 Python 进程同样继承作业。
$jobTypes = Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'ProcessJob.cs'))) -PassThru
$jobType = $jobTypes | Where-Object { $_.Name -eq 'TaigerProcessJob' }
if (-not $jobType) { throw 'Windows process job could not be loaded' }
$processJob = $jobType::AttachKillOnExit()
$portProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8000)
$portProbe.ExclusiveAddressUse = $true
try { $portProbe.Start() } finally { $portProbe.Stop() }
$children = @()
try {
    $children += Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'scripts.process_logs', '--name', 'app', '--', ('"' + $pythonExe + '"'), '-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        if ($children[0].HasExited) { throw 'Application exited before becoming ready' }
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Application readiness timed out' }
    $children += Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'scripts.process_logs', '--name', 'frpc', '--', ('"' + $frpcExe + '"'), '-c', ('"' + $frpcConfig + '"')) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    Write-DeploymentLog 'Application and frpc started'
    while ($true) {
        foreach ($child in $children) {
            if ($child.HasExited) { throw "Deployment child exited: $($child.Id) / $($child.ExitCode)" }
        }
        Start-Sleep -Seconds 2
    }
} finally {
    foreach ($child in $children) {
        if (-not $child.HasExited) { Stop-Process -Id $child.Id -ErrorAction SilentlyContinue }
    }
}
