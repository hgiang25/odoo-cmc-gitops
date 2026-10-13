[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [switch]$SkipImageBuild
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = $PSScriptRoot
$Context = 'docker-desktop'
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed (exit $LASTEXITCODE)." }
}
function Invoke-Kube {
    param([string[]]$Arguments)
    Invoke-Checked 'kubectl' (@('--context', $Context) + $Arguments)
}
function Apply-Object {
    param($Object)
    $Object | ConvertTo-Json -Depth 20 -Compress | & kubectl --context $Context apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'Failed to apply generated object.' }
}
function New-RandomPassword {
    $Bytes = New-Object byte[] 32
    $Generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
    [Convert]::ToBase64String($Bytes)
}
function Ensure-Secret {
    param([string]$Namespace, [string]$Name, [hashtable]$Values)
    $Existing = & kubectl --context $Context -n $Namespace get secret $Name --ignore-not-found -o json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read Secrets.' }
    if (-not $Existing) {
        Apply-Object @{apiVersion='v1';kind='Secret';metadata=@{name=$Name;namespace=$Namespace};type='Opaque';stringData=$Values}
    } else {
        $Data = ($Existing | ConvertFrom-Json).data
        foreach ($Key in $Values.Keys) {
            if (-not $Data.PSObject.Properties[$Key]) { throw "Existing Secret '$Name' lacks '$Key'; migrate it before deploying." }
        }
    }
}
foreach ($Tool in @('kubectl', 'python')) { Get-Command $Tool -ErrorAction Stop | Out-Null }
Invoke-Checked 'python' @((Join-Path $Root 'validate.py'))
if ($ValidateOnly) { return }
foreach ($Tool in @('docker', 'helm')) { Get-Command $Tool -ErrorAction Stop | Out-Null }
$Contexts = & kubectl config get-contexts -o name
if ($LASTEXITCODE -ne 0 -or $Contexts -notcontains $Context) {
    throw "Enable Kubernetes in Docker Desktop first: context '$Context' is missing. No cluster was changed."
}
Invoke-Kube @('cluster-info')
$Namespace = & kubectl --context $Context get namespace odoo-system --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect application namespace.' }
if ($Namespace) {
    $OldWeb = & kubectl --context $Context -n odoo-system get deployment odoo-web --ignore-not-found -o name
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect previous web Deployment.' }
    if ($OldWeb) { throw 'Migrate the previous odoo-web Deployment before creating the Rollout; keep PVCs.' }
    $Lock = & kubectl --context $Context -n odoo-system get configmap odoo-backup-lock --ignore-not-found -o name
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect backup lock.' }
    if ($Lock) { throw 'Backup is running or requires recovery; inspect odoo-backup-lock before deploying.' }
    $RolloutCRD = & kubectl --context $Context get crd rollouts.argoproj.io --ignore-not-found -o name
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Rollout CRD.' }
    if ($RolloutCRD) {
        $Phase = & kubectl --context $Context -n odoo-system get rollout odoo-web-rollout --ignore-not-found -o 'jsonpath={.status.phase}'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Rollout phase.' }
        if ($Phase -and $Phase -ne 'Healthy') { throw "Existing Rollout phase is '$Phase'; finish or abort it before redeploying." }
    }
}

if (-not $SkipImageBuild) {
    Invoke-Checked 'docker' @('build', '-t', 'odoo-queue:17.0-lab', (Join-Path $Root 'image'))
    Invoke-Checked 'docker' @('build', '-t', 'odoo-backup:15-lab', (Join-Path $Root 'backup'))
}
Invoke-Kube @('apply', '-f', (Join-Path $Root 'base/00-namespace.yaml'))
Apply-Object @{apiVersion='v1';kind='Namespace';metadata=@{name='apisix'}}
Ensure-Secret 'odoo-system' 'odoo-secrets' @{
    POSTGRES_USER='odoo';POSTGRES_PASSWORD=(New-RandomPassword)
    ADMIN_PASSWORD=(New-RandomPassword);ADMIN_LOGIN_PASSWORD=(New-RandomPassword)
}
Ensure-Secret 'apisix' 'apisix-admin-credentials' @{admin=(New-RandomPassword);viewer=(New-RandomPassword)}
Invoke-Kube @('apply', '-f', (Join-Path $Root 'infrastructure/etcd.yaml'))
Invoke-Kube @('-n','apisix','rollout','status','statefulset/apisix-etcd','--timeout=180s')
Invoke-Checked 'helm' @('upgrade','--install','apisix','apisix','--repo','https://apache.github.io/apisix-helm-chart',
    '--version','2.10.0','--kube-context',$Context,'-n','apisix','-f',(Join-Path $Root 'helm/apisix-values.yaml'),'--wait','--timeout','10m')
Invoke-Checked 'helm' @('upgrade','--install','argo-rollouts','argo-rollouts','--repo','https://argoproj.github.io/argo-helm',
    '--version','2.43.6','--kube-context',$Context,'-n','argo-rollouts','--create-namespace','--wait','--timeout','5m')
Invoke-Checked 'helm' @('upgrade','--install','keda','keda','--repo','https://kedacore.github.io/charts',
    '--version','2.18.0','--kube-context',$Context,'-n','keda','--create-namespace','--wait','--timeout','5m')
Invoke-Checked 'helm' @('upgrade','--install','metrics-server','metrics-server','--repo','https://kubernetes-sigs.github.io/metrics-server',
    '--version','3.12.2','--kube-context',$Context,'-n','kube-system','--set','args[0]=--kubelet-insecure-tls','--wait','--timeout','5m')
Invoke-Kube @('wait','--for=condition=Established','crd/apisixroutes.apisix.apache.org',
    'crd/rollouts.argoproj.io','crd/scaledobjects.keda.sh','--timeout=120s')
foreach ($File in @('base/01-secrets-and-configmap.yaml','base/02-storage.yaml','base/03-postgres.yaml')) {
    if ($File -eq 'base/02-storage.yaml') {
        # Apply the local overlay's PVCs only; no app can start before DB initialization.
        $Rendered = & kubectl kustomize (Join-Path $Root 'overlays/local')
        if ($LASTEXITCODE -ne 0) { throw 'Kustomize render failed.' }
        $Storage = $Rendered | & python (Join-Path $Root 'manifest.py') storage
        if ($LASTEXITCODE -ne 0) { throw 'Cannot select storage manifests.' }
        $Storage | & kubectl --context $Context apply -f -
        if ($LASTEXITCODE -ne 0) { throw 'Cannot apply PVCs.' }
    } else { Invoke-Kube @('apply','-f',(Join-Path $Root $File)) }
}
Invoke-Kube @('-n','odoo-system','rollout','status','statefulset/postgres','--timeout=180s')
$Initialized = & kubectl --context $Context -n odoo-system get job odoo-init-db --ignore-not-found -o 'jsonpath={.status.succeeded}'
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect DB initialization Job.' }
if ($Initialized -ne '1') {
    Invoke-Kube @('apply','-f',(Join-Path $Root 'base/12-init-db.yaml'))
    try { Invoke-Kube @('-n','odoo-system','wait','--for=condition=complete','job/odoo-init-db','--timeout=900s') }
    catch { & kubectl --context $Context -n odoo-system logs job/odoo-init-db; throw }
}
Invoke-Kube @('apply','--dry-run=server','-k',(Join-Path $Root 'overlays/local'))
Invoke-Kube @('apply','-k',(Join-Path $Root 'overlays/local'))
foreach ($Deployment in @('odoo-worker','odoo-cron','odoo-queue-runner')) {
    Invoke-Kube @('-n','odoo-system','rollout','status',"deployment/$Deployment",'--timeout=300s')
}
Invoke-Kube @('-n','odoo-system','wait','--for=condition=Available','rollout/odoo-web-rollout','--timeout=300s')
Invoke-Kube @('-n','odoo-system','wait','--for=condition=Ready','scaledobject/odoo-web-autoscaler','scaledobject/odoo-worker-autoscaler','--timeout=180s')
Invoke-Kube @('-n','odoo-system','get','pods,svc,pvc,rollouts,scaledobjects,apisixroutes')
Write-Host 'Deployment ready. Add 127.0.0.1 odoo.local to hosts, then run .\k8s\port-forward.ps1.'
