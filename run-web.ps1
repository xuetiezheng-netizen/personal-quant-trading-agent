[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# This launcher is intentionally fixed to localhost and the repository's
# bundled virtual environment.  It is safe to start from a desktop shortcut.
$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pythonwPath = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$webScriptPath = Join-Path $repoRoot "scripts\web_app.py"
$url = "http://127.0.0.1:8765/"
$healthUrl = "http://127.0.0.1:8765/api/health"

function Fail-WebLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,
        [int]$ExitCode = 1
    )

    [Console]::Error.WriteLine($Message)
    exit $ExitCode
}

if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
    Fail-WebLauncher "未找到项目虚拟环境的 pythonw.exe：$pythonwPath。请先完成项目安装。" 2
}
if (-not (Test-Path -LiteralPath $webScriptPath -PathType Leaf)) {
    Fail-WebLauncher "未找到统一入口程序：$webScriptPath。请检查项目文件是否完整。" 3
}

function Get-HealthResponse {
    try {
        return Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 1 -ErrorAction Stop
    }
    catch {
        return $null
    }
}

$existingHealth = Get-HealthResponse
if ($null -ne $existingHealth -and $existingHealth.app_id -eq "personal-quant-trading-agent-web") {
    # Reuse the existing instance rather than starting a second helper.
}
else {
    # A listener that is not our app must not be overwritten.
    $portInUse = $false
    try {
        $portInUse = [bool](Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
    }
    catch {
        # Older Windows builds may not expose Get-NetTCPConnection; the health
        # probe below still detects a conflicting HTTP listener.
        try {
            $portInUse = [bool](Test-NetConnection -ComputerName "127.0.0.1" -Port 8765 -InformationLevel Quiet -WarningAction SilentlyContinue)
        }
        catch {
            $portInUse = $false
        }
    }
    if ($portInUse) {
        Fail-WebLauncher "端口 8765 已被其他程序占用，未启动量化研究台。请关闭占用程序后重试。" 4
    }

    try {
        # The server is a hidden helper; Chrome is opened visibly below.
        $null = Start-Process -FilePath $pythonwPath `
            -ArgumentList @("-X", "utf8", $webScriptPath) `
            -WorkingDirectory $repoRoot `
            -WindowStyle Hidden `
            -PassThru
    }
    catch {
        Fail-WebLauncher "启动本地服务失败：$($_.Exception.Message)" 5
    }

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 300
        $health = Get-HealthResponse
        if ($null -ne $health -and $health.app_id -eq "personal-quant-trading-agent-web") {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        Fail-WebLauncher "本地服务在等待时间内没有启动成功，请检查项目虚拟环境和端口 8765。" 6
    }
}

function Find-Chrome {
    $command = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path ${env:ProgramFiles} "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:LOCALAPPDATA} "Google\Chrome\Application\chrome.exe")
    )
    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    return $null
}

$chromePath = Find-Chrome
if ([string]::IsNullOrWhiteSpace($chromePath)) {
    Fail-WebLauncher "未找到 Google Chrome，服务已启动但没有打开浏览器。请安装 Chrome 后重新双击 run-web.cmd。" 7
}

try {
    Start-Process -FilePath $chromePath -ArgumentList @($url) | Out-Null
}
catch {
    Fail-WebLauncher "打开 Chrome 失败：$($_.Exception.Message)" 8
}

exit 0
