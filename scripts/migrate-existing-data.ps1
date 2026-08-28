[CmdletBinding()]
param(
    [string]$PulseRoot = 'D:\Works\Python\BellennePulse',
    [string]$EchoRoot = 'D:\Works\Python\WBAnsewer',
    [string]$VectorRoot = 'D:\Works\Python\AdsStatistics',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

$files = @(
    [pscustomobject]@{ Module = 'Pulse'; Source = Join-Path $PulseRoot 'data\bellennepulse.db'; Container = 'bellenneone-pulse'; Destination = '/app/data/bellennepulse.db'; Required = $true; IsDatabase = $true },
    [pscustomobject]@{ Module = 'Echo'; Source = Join-Path $EchoRoot 'data\app.db'; Container = 'bellenneone-echo'; Destination = '/app/data/bellenneecho.db'; Required = $true; IsDatabase = $true },
    [pscustomobject]@{ Module = 'Vector'; Source = Join-Path $VectorRoot 'data\ads_statistics.db'; Container = 'bellenneone-vector'; Destination = '/data/bellennevector.db'; Required = $false; IsDatabase = $true },
    [pscustomobject]@{ Module = 'Vector'; Source = Join-Path $VectorRoot 'data\.token_key'; Container = 'bellenneone-vector'; Destination = '/data/.token_key'; Required = $false; IsDatabase = $false },
    [pscustomobject]@{ Module = 'Vector'; Source = Join-Path $VectorRoot 'data\.session_key'; Container = 'bellenneone-vector'; Destination = '/data/.session_key'; Required = $false; IsDatabase = $false }
)

$missingRequired = $files | Where-Object { $_.Required -and -not (Test-Path -LiteralPath $_.Source -PathType Leaf) }
if ($missingRequired) {
    $missingRequired | ForEach-Object { Write-Error "Не найден обязательный файл: $($_.Source)" }
    throw 'План миграции неполон.'
}

$available = $files | Where-Object { Test-Path -LiteralPath $_.Source -PathType Leaf }
$available | Select-Object Module, Source, Destination | Format-Table -AutoSize

foreach ($item in ($available | Where-Object IsDatabase)) {
    $walPath = "$($item.Source)-wal"
    if ((Test-Path -LiteralPath $walPath -PathType Leaf) -and (Get-Item -LiteralPath $walPath).Length -gt 0) {
        throw "Обнаружен непустой WAL: $walPath. Остановите старый стек и выполните checkpoint SQLite."
    }
}

if (-not $Apply) {
    Write-Output 'Dry run завершён. Для переноса остановите старые стеки и повторите команду с -Apply.'
    exit 0
}

Set-Location -LiteralPath $workspace
& docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw 'docker compose config завершился ошибкой.' }

& docker compose create shell pulse echo vector
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить контейнеры BellenneOne.' }

& docker compose stop gateway pulse echo vector
if ($LASTEXITCODE -ne 0) { throw 'Не удалось остановить целевые контейнеры.' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $workspace "backups\migration-$stamp"
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

$backupTargets = @(
    [pscustomobject]@{ Module = 'pulse'; Container = 'bellenneone-pulse'; Path = '/app/data/.' },
    [pscustomobject]@{ Module = 'echo'; Container = 'bellenneone-echo'; Path = '/app/data/.' },
    [pscustomobject]@{ Module = 'vector'; Container = 'bellenneone-vector'; Path = '/data/.' }
)

foreach ($target in $backupTargets) {
    $destination = Join-Path $backupRoot $target.Module
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    & docker cp "$($target.Container):$($target.Path)" $destination
    if ($LASTEXITCODE -ne 0) { throw "Не удалось сохранить резервную копию $($target.Module)." }
}

& docker compose run --rm --no-deps --user root pulse sh -c 'rm -f /app/data/bellennepulse.db-wal /app/data/bellennepulse.db-shm'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить SQLite-файлы Pulse.' }
& docker compose run --rm --no-deps --user root echo sh -c 'rm -f /app/data/bellenneecho.db-wal /app/data/bellenneecho.db-shm'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить SQLite-файлы Echo.' }
if ($available | Where-Object { $_.Module -eq 'Vector' -and $_.IsDatabase }) {
    & docker compose run --rm --no-deps --user root vector sh -c 'rm -f /data/bellennevector.db-wal /data/bellennevector.db-shm'
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить SQLite-файлы Vector.' }
}

foreach ($item in $available) {
    & docker cp $item.Source "$($item.Container):$($item.Destination)"
    if ($LASTEXITCODE -ne 0) { throw "Не удалось перенести $($item.Source)." }
}

& docker compose run --rm --no-deps --user root pulse sh -c 'chown -R bellennepulse:bellennepulse /app/data'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось восстановить владельца данных Pulse.' }
& docker compose run --rm --no-deps --user root echo sh -c 'chown -R bellenneecho:bellenneecho /app/data'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось восстановить владельца данных Echo.' }
& docker compose run --rm --no-deps --user root vector sh -c 'chown -R bellennevector:bellennevector /data'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось восстановить владельца данных Vector.' }

& docker compose up -d
if ($LASTEXITCODE -ne 0) { throw 'Данные перенесены, но BellenneOne не удалось запустить.' }

Write-Output "Миграция завершена. Резервная копия: $backupRoot"
