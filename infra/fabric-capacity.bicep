// Provisions a Microsoft Fabric capacity (F SKU) in the target resource group.
// The capacity is created in the "Active" state and immediately billable.
// To pause/resume billing, use:
//   az fabric capacity suspend --resource-group <rg> --capacity-name <name>
//   az fabric capacity resume  --resource-group <rg> --capacity-name <name>

@description('Name of the Fabric capacity. Lowercase letters and digits only, 3-63 chars. Must be globally unique.')
@minLength(3)
@maxLength(63)
param capacityName string

@description('Azure region. Must support Fabric capacities (e.g. westeurope, northeurope, eastus, eastus2, westus2, westus3, southeastasia).')
param location string = resourceGroup().location

@description('Fabric SKU. F2 is the smallest; F4 is the typical demo size. See https://learn.microsoft.com/fabric/enterprise/buy-subscription for pricing.')
@allowed([
  'F2'
  'F4'
  'F8'
  'F16'
  'F32'
  'F64'
  'F128'
  'F256'
  'F512'
  'F1024'
  'F2048'
])
param sku string = 'F4'

@description('UPNs (preferred) or object IDs of capacity administrators. At least one required.')
@minLength(1)
param adminMembers array

@description('Optional resource tags.')
param tags object = {}

resource capacity 'Microsoft.Fabric/capacities@2023-11-01' = {
  name: capacityName
  location: location
  tags: tags
  sku: {
    name: sku
    tier: 'Fabric'
  }
  properties: {
    administration: {
      members: adminMembers
    }
  }
}

output capacityId string = capacity.id
output capacityName string = capacity.name
output sku string = capacity.sku.name
output location string = capacity.location
