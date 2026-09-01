[CmdletBinding()]
param(
    [ValidateSet("CaptureClose", "Research")]
    [string]$Mode = "Research"
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is the directory containing this launcher, so it is independent
# of the user's current PowerShell directory.
$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$realScriptPath = Join-Path $repoRoot "scripts\run_real.py"

function ConvertFrom-Base64Utf8 {
    param([Parameter(Mandatory = $true)][string]$Value)
    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine((ConvertFrom-Base64Utf8 "5om+5LiN5Yiw6aG555uuIFB5dGhvbiDnjq/looPvvJo=") + $pythonPath + (ConvertFrom-Base64Utf8 "44CC6K+35YWI56Gu6K6k6aG555uu5bey5a6J6KOF5a6M5oiQ44CC"))
    exit 2
}

if (-not (Test-Path -LiteralPath $realScriptPath -PathType Leaf)) {
    [Console]::Error.WriteLine((ConvertFrom-Base64Utf8 "5om+5LiN5Yiw55yf5a6e6KGM5oOF5YWl5Y+j77ya") + $realScriptPath + (ConvertFrom-Base64Utf8 "44CC6K+356Gu6K6k6aG555uu5paH5Lu25a6M5pW044CC"))
    exit 3
}

if ($Mode -eq "CaptureClose") {
    Write-Host (ConvertFrom-Base64Utf8 "5YeG5aSH5oqT5Y+W5paw5rWq5YWs5byA5pS255uY5b+r54Wn44CC5LuF5Zyo5YyX5Lqs5pe26Ze05bel5L2c5pelIDE1OjAxLTE1OjE1IOi/kOihjO+8m+S4jeS8muS6pOaYk++8jOS5n+S4jeS8muiwg+eUqCBBSeOAgg==")
}
else {
    Write-Host (ConvertFrom-Base64Utf8 "5YeG5aSH5L2/55So5pyA6L+R5LiA5Lu95paw5rWq5YWs5byA5pS255uY5b+r54Wn55Sf5oiQ56CU56m25oql5ZGK44CC5LiN5Lya6K+75Y+WIC5lbnbjgIHkuI3kvJrosIPnlKggQUnvvIzkuZ/kuI3kvJrkuqTmmJPjgII=")
}

# -X utf8 keeps Chinese prompts and the generated Markdown stable on Windows.
& $pythonPath -X utf8 $realScriptPath --mode $Mode --repo-root $repoRoot
$exitCode = $LASTEXITCODE
if ($null -eq $exitCode) {
    $exitCode = 0
}
exit $exitCode
