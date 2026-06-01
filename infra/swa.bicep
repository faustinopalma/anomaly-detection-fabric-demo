// Provisions an Azure Static Web App (Free SKU) to host the simulator control
// panel in `webapp/`. The site is a no-build static bundle (HTML/CSS/JS); all
// dynamic behaviour lives in the cloud simulator's control API (the Container
// App `ca-simulator`), which the page calls directly from the browser.
//
// Deploy:
//   az deployment group create -g <rg> -f infra/swa.bicep \
//     -p name=swa-anomaly-sim location=westeurope
//
// After deployment, publish the static files with the SWA CLI or
// `az staticwebapp` (see webapp/README.md). There is no GitHub integration
// configured here so the deployment stays manual and unattended-safe.
//
// IMPORTANT — manual step (not done by this template):
//   The control panel needs the Container App `ca-simulator` to expose its
//   control port (SIM_CONTROL_PORT, default 8080) via *external* ingress and
//   to run with SIM_CONTROL_ENABLED=1 and a SIM_CONTROL_API_KEY set. Enabling
//   external ingress and redeploying the live container is intentionally left
//   as a manual operation so the running demo is never changed unattended:
//     az containerapp ingress enable -g rg-fabric-demo -n ca-simulator \
//       --type external --target-port 8080 --transport auto
//     az containerapp update -g rg-fabric-demo -n ca-simulator \
//       --set-env-vars SIM_CONTROL_ENABLED=1 SIM_CONTROL_PORT=8080 \
//                      SIM_CONTROL_API_KEY=<secret> \
//                      SIM_CONTROL_CORS_ORIGINS=<swa-url>

@description('Name of the Static Web App. 1-60 chars.')
@minLength(1)
@maxLength(60)
param name string

@description('Azure region for the Static Web App. Free SKU is available in a subset of regions (e.g. westeurope, eastus2, eastasia).')
@allowed([
  'westeurope'
  'eastus2'
  'eastasia'
  'centralus'
  'westus2'
])
param location string = 'westeurope'

@description('Optional resource tags.')
param tags object = {}

resource swa 'Microsoft.Web/staticSites@2023-12-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    // No repository wiring — content is pushed manually via the SWA CLI.
    allowConfigFileUpdates: true
    stagingEnvironmentPolicy: 'Disabled'
  }
}

output staticSiteId string = swa.id
output staticSiteName string = swa.name
output defaultHostname string = swa.properties.defaultHostname
