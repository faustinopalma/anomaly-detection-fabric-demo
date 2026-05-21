"""Download a Fabric notebook definition and print cell outputs / errors."""
from __future__ import annotations
import argparse, base64, json, os, sys, time
from pathlib import Path
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential

API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("name", help="Notebook display name in Fabric")
    p.add_argument("--save", type=Path, default=None, help="Optional path to save ipynb")
    p.add_argument("--errors-only", action="store_true")
    args = p.parse_args()

    repo = Path(__file__).resolve().parent.parent
    load_dotenv(repo / ".env", override=True)
    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    cred = get_credential(tenant, FABRIC_SCOPE, repo)
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {cred.get_token(FABRIC_SCOPE).token}"

    ws = next(w for w in s.get(f"{API}/workspaces").json()["value"] if w["displayName"] == ws_name)
    nb = next(n for n in s.get(f"{API}/workspaces/{ws['id']}/notebooks").json()["value"] if n["displayName"] == args.name)
    print(f"[ok] {args.name} -> {nb['id']}")

    r = s.post(f"{API}/workspaces/{ws['id']}/items/{nb['id']}/getDefinition?format=ipynb")
    if r.status_code == 202:
        loc = r.headers["Location"]
        while True:
            rr = s.get(loc); rr.raise_for_status()
            body = rr.json() if rr.content else {}
            st = (body.get("status") or "").lower()
            if st in ("succeeded", "completed"):
                # result url
                res = s.get(loc + "/result") if not loc.endswith("/result") else rr
                r = res
                break
            if st == "failed":
                raise SystemExit(f"failed: {body}")
            time.sleep(2)
    r.raise_for_status()
    parts = r.json()["definition"]["parts"]
    nb_part = next(p for p in parts if p["path"].endswith(".ipynb"))
    ipynb = json.loads(base64.b64decode(nb_part["payload"]).decode("utf-8"))

    if args.save:
        args.save.write_text(json.dumps(ipynb, indent=2), encoding="utf-8")
        print(f"[saved] {args.save}")

    for i, cell in enumerate(ipynb.get("cells", []), start=1):
        if cell.get("cell_type") != "code":
            continue
        outs = cell.get("outputs", [])
        if not outs:
            continue
        has_err = any(o.get("output_type") == "error" for o in outs)
        if args.errors_only and not has_err:
            continue
        src = "".join(cell.get("source", []))
        first = src.splitlines()[0] if src.strip() else "(empty)"
        print(f"\n=== cell {i} ===")
        print(f"  source[0]: {first[:100]}")
        for o in outs:
            ot = o.get("output_type")
            if ot == "error":
                print(f"  [ERROR] {o.get('ename')}: {o.get('evalue')}")
                for line in o.get("traceback", []):
                    print("    " + line)
            elif ot == "stream":
                txt = "".join(o.get("text", []))
                print(f"  [stream:{o.get('name')}] {txt[:500]}")
            elif ot in ("execute_result", "display_data"):
                data = o.get("data", {})
                if "text/plain" in data:
                    txt = "".join(data["text/plain"]) if isinstance(data["text/plain"], list) else data["text/plain"]
                    print(f"  [result] {txt[:300]}")


if __name__ == "__main__":
    main()
