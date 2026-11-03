param(
    [string]$InputFile,
    [string]$Resume,
    [string]$HomeDirectory,
    [string]$Python,
    [switch]$RetryFailed
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
if (-not $HomeDirectory) { $HomeDirectory = Join-Path $repo '.music-bridge' }
if (-not $Python) { $Python = Join-Path $repo '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw '请先按 README 安装 .venv，或使用 -Python 指定已安装依赖的 Python 路径' }
if ($InputFile -and $Resume) { throw '-InputFile 和 -Resume 只能选一个' }
$cliArgs = @('-m', 'music_bridge.cli', '--home', $HomeDirectory, '--progress')
if ($Resume) {
    $cliArgs += @('jobs', 'resume', $Resume)
    if (-not $RetryFailed) { $cliArgs += '--skip-failed' }
} elseif ($InputFile) {
    $cliArgs += @('migrate', $InputFile)
} else {
    $cliArgs += @('jobs', 'status')
}
Push-Location $repo
try {
    & $Python @cliArgs
    $result = $LASTEXITCODE
} finally { Pop-Location }
exit $result
