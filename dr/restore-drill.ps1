[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^\d{8}T\d{6}Z$')][string]$BackupSet,
    [ValidatePattern('^odoo17_restore_drill(_[a-z0-9_]+)?$')][string]$Database = 'odoo17_restore_drill',
    [string]$Context = 'docker-desktop',
    [string]$Template = (Join-Path $PSScriptRoot 'restore-job.yaml')
)
$ErrorActionPreference = 'Stop'
$Contexts = & kubectl config get-contexts -o name
if ($LASTEXITCODE -ne 0 -or $Contexts -notcontains $Context) { throw "Missing '$Context' context." }
if ($Context -ne 'docker-desktop' -and $Template -eq (Join-Path $PSScriptRoot 'restore-job.yaml')) {
    throw 'Cloud restore requires -Template pointing to the generated CMC restore-job.yaml (registry image).'
}
$Manifest = & python (Join-Path $PSScriptRoot '../manifest.py') restore --template $Template --backup-set $BackupSet --database $Database
if ($LASTEXITCODE -ne 0) { throw 'Cannot render restore Job.' }
$Manifest | & kubectl --context $Context create -f -
if ($LASTEXITCODE -ne 0) { throw 'Cannot create restore drill Job.' }
$Hash = [System.Security.Cryptography.SHA256]::Create()
try { $Suffix = ([BitConverter]::ToString($Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Database)))).Replace('-', '').ToLower().Substring(0, 8) }
finally { $Hash.Dispose() }
$JobName = 'odoo-restore-drill-' + $BackupSet.ToLower() + '-' + $Suffix
& kubectl --context $Context -n odoo-system wait --for=condition=complete "job/$JobName" --timeout=900s
$WaitExit = $LASTEXITCODE
& kubectl --context $Context -n odoo-system logs "job/$JobName"
if ($WaitExit -ne 0 -or $LASTEXITCODE -ne 0) { throw 'Restore drill failed; retained database/volume for inspection.' }
