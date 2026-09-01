[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# $PSScriptRoot points to this file, so the launcher works from any current directory.
$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$demoScriptPath = Join-Path $repoRoot "scripts\run_demo.py"

function ConvertFrom-Base64Utf8 {
    param([Parameter(Mandatory = $true)][string]$Value)
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    $message = (ConvertFrom-Base64Utf8 "5pyq5om+5Yiw6aG555uu6Jma5ouf546v5aKD77ya") + $pythonPath + (ConvertFrom-Base64Utf8 "44CC6K+35YWI5Zyo6aG555uu55uu5b2V5Yib5bu6IC52ZW5277yM5oiW6YeN5paw5omn6KGM546v5aKD5Yid5aeL5YyW44CC")
    [Console]::Error.WriteLine($message)
    exit 2
}

if (-not (Test-Path -LiteralPath $demoScriptPath -PathType Leaf)) {
    $message = (ConvertFrom-Base64Utf8 "5pyq5om+5Yiw5ryU56S66ISa5pys77ya") + $demoScriptPath + (ConvertFrom-Base64Utf8 "44CC6K+35qOA5p+l6aG555uu5paH5Lu25piv5ZCm5a6M5pW044CC")
    [Console]::Error.WriteLine($message)
    exit 3
}

# -X utf8 makes Python read and write the report as UTF-8 regardless of the shell locale.
& $pythonPath -X utf8 $demoScriptPath
$exitCode = $LASTEXITCODE
if ($null -eq $exitCode) {
    $exitCode = 0
}
exit $exitCode
