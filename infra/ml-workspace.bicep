// Azure Machine Learning workspace + dependencies + GPU compute cluster.
//
// Provisions an Azure ML workspace (with its mandatory Storage, Key Vault,
// Application Insights / Log Analytics dependencies) and an A100 GPU
// compute cluster that scales to zero when idle, so cost is incurred only
// while a training job runs.
//
// Validate:   bicep build infra/ml-workspace.bicep
// Deploy:     az deployment group create -g <rg> -f infra/ml-workspace.bicep
//
// Compute SKU note: the model is tiny (~162K params) so a small CPU cluster
// trains both machines in seconds. Modern GPU families (A100/T4/V100/H100)
// have an Azure ML *dedicated* (BatchAI) quota of 0 in all candidate regions
// even though the Microsoft.Compute regional A100 quota is 24 in italynorth.
// To train on GPU, first request the AML quota
//   "NCADSA100v4 Family Cluster Dedicated vCPUs" >= 24 (italynorth),
// then set computeVmSize='Standard_NC24ads_A100_v4'.

targetScope = 'resourceGroup'

@description('Base name used to derive resource names. Lowercase letters/numbers.')
@minLength(3)
@maxLength(16)
param baseName string = 'anomalyml'

@description('Location for all resources.')
param location string = resourceGroup().location

@description('VM size for the training compute cluster. Default CPU; set to a GPU SKU once AML GPU quota is granted.')
param computeVmSize string = 'Standard_DS3_v2'

@description('Maximum nodes for the cluster.')
@minValue(1)
@maxValue(4)
param computeMaxNodes int = 1

@description('Idle seconds before the cluster scales back down to zero nodes.')
param idleSecondsBeforeScaledown int = 180

@description('Tags applied to all resources.')
param tags object = {
  project: 'anomaly-detection-fabric-demo'
  purpose: 'per-machine-ae-training'
}

var suffix = uniqueString(resourceGroup().id, baseName)
var storageName = toLower('st${baseName}${take(suffix, 8)}')
var keyVaultName = toLower('kv${baseName}${take(suffix, 8)}')
var appInsightsName = '${baseName}-ai'
var logAnalyticsName = '${baseName}-law'
var workspaceName = '${baseName}-mlw'
var computeName = 'cpu-cluster'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    encryption: {
      services: {
        blob: {
          enabled: true
        }
        file: {
          enabled: true
        }
      }
      keySource: 'Microsoft.Storage'
    }
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    accessPolicies: []
  }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource mlWorkspace 'Microsoft.MachineLearningServices/workspaces@2024-04-01' = {
  name: workspaceName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'Anomaly Detection ML (per-machine AE training)'
    storageAccount: storage.id
    keyVault: keyVault.id
    applicationInsights: appInsights.id
    publicNetworkAccess: 'Enabled'
  }
}

resource gpuCluster 'Microsoft.MachineLearningServices/workspaces/computes@2024-04-01' = {
  parent: mlWorkspace
  name: computeName
  location: location
  tags: tags
  properties: {
    computeType: 'AmlCompute'
    properties: {
      vmSize: computeVmSize
      vmPriority: 'Dedicated'
      scaleSettings: {
        minNodeCount: 0
        maxNodeCount: computeMaxNodes
        nodeIdleTimeBeforeScaleDown: 'PT${idleSecondsBeforeScaledown}S'
      }
    }
  }
}

output workspaceName string = mlWorkspace.name
output computeName string = gpuCluster.name
output storageAccount string = storage.name
output keyVault string = keyVault.name
output resourceGroup string = resourceGroup().name
output location string = location
