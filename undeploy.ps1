# Explicit destructive local cleanup. Controllers may be shared; remove separately.
[CmdletBinding(SupportsShouldProcess=$true, ConfirmImpact='High')]
param([switch]$DeleteLocalData)
$ErrorActionPreference = 'Stop'
if (-not $DeleteLocalData) {
    throw 'This deletes the local Odoo namespace, database and PVCs. Pass -DeleteLocalData to request cleanup.'
}
if ($PSCmdlet.ShouldProcess('docker-desktop/odoo-system', 'Delete namespace and all Odoo data')) {
    & kubectl --context docker-desktop delete namespace odoo-system --ignore-not-found --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw 'Cleanup did not complete; inspect namespace finalizers.' }
    Write-Host 'Local Odoo namespace deleted. Shared Helm controllers remain installed.'
}
