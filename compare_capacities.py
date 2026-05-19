import os
import requests
import subprocess
import json
from dotenv import load_dotenv
from azure.identity import AzureCliCredential

def main():
    load_dotenv()
    workspace_name = os.getenv('FABRIC_WORKSPACE_NAME')
    capacity_name = os.getenv('FABRIC_CAPACITY_NAME')

    print(f"Workspace Name: {workspace_name}")
    print(f"Capacity Name: {capacity_name}")

    credential = AzureCliCredential()
    token = credential.get_token("https://api.fabric.microsoft.com/.default")
    headers = {"Authorization": f"Bearer {token.token}"}

    # 3. Get Workspace ID
    ws_resp = requests.get("https://api.fabric.microsoft.com/v1/workspaces", headers=headers)
    ws_resp.raise_for_status()
    workspaces = ws_resp.json().get('value', [])
    workspace = next((w for w in workspaces if w['displayName'] == workspace_name), None)
    
    if not workspace:
        print(f"Workspace {workspace_name} not found.")
        return
    
    workspace_id = workspace['id']
    print(f"Workspace ID: {workspace_id}")

    # 4. Get Workspace Details (Capacity ID)
    details_resp = requests.get(f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}", headers=headers)
    details_resp.raise_for_status()
    details = details_resp.json()
    fabric_capacity_id = details.get('capacityId', 'Not Found')
    print(f"Fabric API Capacity ID: {fabric_capacity_id}")

    # 5. Get ARM ID from Azure CLI
    try:
        arm_output = subprocess.check_output(
            f'az resource show --resource-group rg-fabric-demo --name {capacity_name} --resource-type Microsoft.Fabric/capacities',
            shell=True, text=True
        )
        arm_data = json.loads(arm_output)
        arm_id = arm_data.get('id', 'Not Found')
        print(f"Azure ARM Capacity ID: {arm_id}")
    except Exception as e:
        print(f"Error getting ARM ID: {e}")

if __name__ == '__main__':
    main()
