[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$LocalPort = 30080
)

$ErrorActionPreference = 'Stop'
$Context = 'docker-desktop'

Get-Command kubectl -ErrorAction Stop | Out-Null

$Contexts = & kubectl config get-contexts -o name
if ($LASTEXITCODE -ne 0 -or $Contexts -notcontains $Context) {
    throw "Kubernetes context '$Context' is unavailable. Enable Kubernetes in Docker Desktop first."
}

& kubectl --context $Context -n apisix get service apisix-gateway | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Service 'apisix-gateway' is unavailable. Run .\k8s\deploy.ps1 first."
}

Write-Host "Forwarding http://odoo.local:$LocalPort to APISIX. Keep this terminal open." -ForegroundColor Green
Write-Host 'Press Ctrl+C to stop the connection.' -ForegroundColor Yellow

& kubectl --context $Context -n apisix port-forward service/apisix-gateway "${LocalPort}:80"
if ($LASTEXITCODE -ne 0) {
    throw "kubectl port-forward failed (exit $LASTEXITCODE)."
}
