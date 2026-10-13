# Run only after confirming the failed backup Job is no longer running.
[CmdletBinding()]
param([string]$Context = 'docker-desktop')
$ErrorActionPreference = 'Stop'
$Lock = & kubectl --context $Context -n odoo-system get configmap odoo-backup-lock -o json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read backup recovery state.' }
$State = (($Lock | ConvertFrom-Json).data.'resume.json' | ConvertFrom-Json)
$Running = & kubectl --context $Context -n odoo-system get pods -l component=disaster-recovery -o json
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect backup Pods.' }
foreach ($Pod in ($Running | ConvertFrom-Json).items) {
    if ($Pod.status.phase -notin @('Succeeded','Failed')) { throw 'A backup Pod is still active; recovery refused.' }
}
foreach ($Property in $State.scales.PSObject.Properties) {
    $Scale = & kubectl --context $Context get --raw $Property.Name
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read workload scale.' }
    $Object = $Scale | ConvertFrom-Json
    $Object.spec.replicas = $Property.Value
    $Object | ConvertTo-Json -Depth 20 -Compress | & kubectl --context $Context replace --raw $Property.Name -f -
    if ($LASTEXITCODE -ne 0) { throw 'Cannot restore replicas.' }
}
foreach ($Property in $State.scalers.PSObject.Properties) {
    $Name = ($Property.Name -split '/')[-1]
    $Current = & kubectl --context $Context -n odoo-system get scaledobject $Name -o json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read ScaledObject.' }
    $Object = $Current | ConvertFrom-Json
    if (-not $Object.metadata.annotations) {
        $Object.metadata | Add-Member -NotePropertyName annotations -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    foreach ($Annotation in $Property.Value.PSObject.Properties) {
        if ($null -eq $Annotation.Value) { $Object.metadata.annotations.PSObject.Properties.Remove($Annotation.Name) }
        else { $Object.metadata.annotations | Add-Member -NotePropertyName $Annotation.Name -NotePropertyValue $Annotation.Value -Force }
    }
    $Object | ConvertTo-Json -Depth 100 -Compress | & kubectl --context $Context replace -f -
    if ($LASTEXITCODE -ne 0) { throw 'Cannot restore autoscaling.' }
}
& kubectl --context $Context -n odoo-system delete configmap odoo-backup-lock
if ($LASTEXITCODE -ne 0) { throw 'Cannot remove completed recovery lock.' }
