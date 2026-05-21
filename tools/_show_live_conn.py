"""Print the LIVE primary connection string of sim_local source and compare to secret file."""
from __future__ import annotations
import os, sys, requests
from pathlib import Path
sys.path.insert(0, "tools")
from _fabric_auth import get_credential
from dotenv import load_dotenv

load_dotenv(Path(".env"), override=True)
API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"
cred = get_credential(os.environ["FABRIC_TENANT_ID"], SCOPE, Path("."))
tok = cred.get_token(SCOPE).token
s = requests.Session(); s.headers.update({"Authorization": f"Bearer {tok}"})
ws = next(w for w in s.get(f"{API}/workspaces").json()["value"] if w["displayName"] == os.environ["FABRIC_WORKSPACE_NAME"])["id"]
es = next(e for e in s.get(f"{API}/workspaces/{ws}/eventstreams").json()["value"] if e["displayName"] == os.environ["FABRIC_EVENTSTREAM_NAME"])["id"]
topo = s.get(f"{API}/workspaces/{ws}/eventstreams/{es}/topology").json()
src = [x for x in topo["sources"] if x["type"] == "CustomEndpoint"][0]
print("source id:", src["id"], "name:", src["name"])
conn = s.get(f"{API}/workspaces/{ws}/eventstreams/{es}/sources/{src['id']}/connection").json()
cs = conn["accessKeys"]["primaryConnectionString"]
print("live host:", cs.split(";")[0])
print("live len :", len(cs))
Path("_live.txt").write_text(cs, encoding="ascii")
secret = Path("_secret.txt").read_text(encoding="ascii").strip() if Path("_secret.txt").exists() else ""
env = Path("_env.txt").read_text(encoding="ascii").strip() if Path("_env.txt").exists() else ""
print(f"live==secret: {cs.strip() == secret}")
print(f"live==env   : {cs.strip() == env}")
