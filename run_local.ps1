# Локальный запуск: читает .env из папки проекта и стартует бота.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $name, $value = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
    }
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg не найден. Установите: winget install Gyan.FFmpeg" -ForegroundColor Yellow
}

python -m bot.main
