#!/usr/bin/env python3
"""Deploy and inspect the Gmail Classifier on any n8n instance through its public API.

    python3 n8n/n8n.py <command> [args]

Needs N8N_URL and N8N_API_KEY (n8n → Settings → n8n API), read from the environment or from `.env` at the
repo root. Workflows and Data Tables are addressed by name or id.

  workflows                                   list workflows (id, published, name)
  deploy <workflow.json> [--publish]          create or update by name; republished if it was published
  publish | unpublish <workflow>
  rename <workflow> <new name>                same id, nodes and credentials; republished if it was published
  remove workflow|table|credential <ref> [--yes]   delete one (refused while a workflow still uses it)
  executions <workflow> [n]                   last n executions: status, start, error, items per node
  tables                                      list Data Tables and their columns
  ensure <spec.json>                          create missing tables and columns (never changes or drops one)
  rows <table>                                every row, one JSON object per line
  add-rows <table> <rows.json> <key> [--yes]  add the rows whose key is not in the table yet
  update-rows <table> <ids> <column>=<value> [--yes]
  delete-rows <table> <ids> [--yes]

Writes to Data Tables are dry runs unless --yes is given.
"""
import inspect
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Fields the public API accepts on create/update: anything else is rejected
WORKFLOW_FIELDS = ("name", "nodes", "connections", "settings")


def die(msg):
    sys.exit(f"✗ {msg}")


def dotenv():
    """KEY=value lines of .env at the repo root, overridden by the environment."""
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip().strip("'\"")
    return {**values, **{k: os.environ[k] for k in values.keys() | {"N8N_URL", "N8N_API_KEY"} if k in os.environ}}


def env():
    """N8N_URL and N8N_API_KEY from the environment, else from .env."""
    values = dotenv()
    missing = [k for k in ("N8N_URL", "N8N_API_KEY") if not values.get(k)]
    if missing:
        die(f"missing {', '.join(missing)} (environment or .env)")
    return values["N8N_URL"].rstrip("/"), values["N8N_API_KEY"]


def api(method, path, body=None, query=None):
    url, key = env()
    full = f"{url}/api/v1{path}" + (f"?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, data=data, method=method,
                                 headers={"X-N8N-API-KEY": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        die(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        die(f"{method} {path}: {e.reason}")
    return json.loads(raw) if raw else None


def paged(path, query=None):
    query, cursor = dict(query or {}), None
    while True:
        page = api("GET", path, query={**query, "limit": 250, **({"cursor": cursor} if cursor else {})})
        yield from page["data"]
        cursor = page.get("nextCursor")
        if not cursor:
            return


def find_workflow(ref):
    found = [w for w in paged("/workflows") if ref in (w["id"], w["name"])]
    if len(found) > 1:
        die(f"several workflows match {ref!r}: use the id")
    return found[0] if found else None


def find_table(ref):
    found = [t for t in paged("/data-tables") if ref in (t["id"], t["name"])]
    if not found:
        die(f"Data Table not found: {ref}")
    return found[0]


def confirmed(args):
    return "--yes" in args


def ids_arg(text):
    try:
        return [int(i) for i in text.split(",")]
    except ValueError:
        die(f"row ids must be comma-separated numbers: {text}")


# ---------- workflows ----------

def cmd_workflows():
    for w in paged("/workflows"):
        print(f"{w['id']}  {'published' if w['active'] else '         '}  {w['name']}")


def cmd_publish(ref, publish=True):
    w = find_workflow(ref) or die(f"workflow not found: {ref}")
    w = api("POST", f"/workflows/{w['id']}/{'activate' if publish else 'deactivate'}")
    print(f"✓ {w['name']}: published={w['active']}")


def resolve_credentials(nodes):
    """Credentials given by name only (portable workflow files) get the id of the instance credential with that
    type and name."""
    wanted = {(t, c["name"]) for n in nodes for t, c in (n.get("credentials") or {}).items() if not c.get("id")}
    if not wanted:
        return nodes
    have = {(c["type"], c["name"]): c["id"] for c in paged("/credentials")}
    missing = [f"{name} ({t})" for t, name in sorted(wanted) if (t, name) not in have]
    if missing:
        die(f"create these credentials in n8n first: {', '.join(missing)}")
    return [{**n, "credentials": {t: c if c.get("id") else {"id": have[(t, c["name"])], "name": c["name"]}
                                  for t, c in n["credentials"].items()}} if n.get("credentials") else n
            for n in nodes]


def cmd_deploy(file, *flags):
    wf = json.loads(Path(file).read_text())
    body = {k: wf[k] for k in WORKFLOW_FIELDS if k in wf}
    body["nodes"] = resolve_credentials(body["nodes"])
    current = find_workflow(wf["name"])
    if current:
        # Credentials picked in the n8n UI are kept for nodes the file leaves without credentials:
        # same node name first, else any online node of the same type (a new Gmail node gets the Gmail account)
        online = [n for n in api("GET", f"/workflows/{current['id']}")["nodes"] if n.get("credentials")]
        by_name = {n["name"]: n["credentials"] for n in online}
        by_type = {n["type"]: n["credentials"] for n in online}
        body["nodes"] = [n if n.get("credentials") else
                         {**n, **({"credentials": c} if (c := by_name.get(n["name"]) or by_type.get(n["type"])) else {})}
                         for n in body["nodes"]]
        api("PUT", f"/workflows/{current['id']}", body)
        print(f"✓ updated {wf['name']} ({current['id']})")
        republish = current["active"] or "--publish" in flags
    else:
        current = api("POST", "/workflows", body)
        print(f"✓ created {wf['name']} ({current['id']})")
        republish = "--publish" in flags
    # n8n ≥ 2: an update leaves the published version unchanged until the workflow is published again
    if republish:
        cmd_publish(current["id"])
    else:
        print("  not published")


def cmd_remove(kind, ref, *flags):
    if kind == "workflow":
        item = find_workflow(ref) or die(f"workflow not found: {ref}")
        path, what = f"/workflows/{item['id']}", f"workflow {item['name']} ({item['id']})"
        if item["active"]:
            die(f"{what} is published: unpublish it first")
    elif kind == "table":
        item = find_table(ref)
        path, what = f"/data-tables/{item['id']}", f"Data Table {item['name']} ({item['id']}, {sum(1 for _ in table_rows(item['id']))} rows)"
    elif kind == "credential":
        found = [c for c in paged("/credentials") if ref in (c["id"], c["name"])]
        if len(found) != 1:
            die(f"{len(found)} credentials match {ref!r}")
        item = found[0]
        users = [f"{w['name']} / {n['name']}" for w in paged("/workflows")
                 for n in api("GET", f"/workflows/{w['id']}")["nodes"]
                 if any(c.get("id") == item["id"] for c in (n.get("credentials") or {}).values())]
        if users:
            die(f"credential {item['name']} is used by: {', '.join(users)}")
        path, what = f"/credentials/{item['id']}", f"credential {item['name']} ({item['type']}, {item['id']})"
    else:
        die("remove what? workflow, table or credential")
    if not confirmed(flags):
        return print(f"✓ dry run: would delete {what} (add --yes)")
    api("DELETE", path)
    print(f"✓ deleted {what}")


def cmd_rename(ref, name):
    w = find_workflow(ref) or die(f"workflow not found: {ref}")
    if find_workflow(name):
        die(f"a workflow is already named {name!r}")
    online = api("GET", f"/workflows/{w['id']}")
    api("PUT", f"/workflows/{w['id']}", {**{k: online[k] for k in WORKFLOW_FIELDS if k in online}, "name": name})
    print(f"✓ renamed {w['name']} → {name} ({w['id']})")
    if w["active"]:
        cmd_publish(w["id"])


def cmd_executions(ref, n="10"):
    w = find_workflow(ref) or die(f"workflow not found: {ref}")
    page = api("GET", "/executions", query={"workflowId": w["id"], "limit": int(n), "includeData": "true"})
    for e in page["data"]:
        result = (e.get("data") or {}).get("resultData") or {}
        nodes = " ".join(f"{name}=" + "/".join(str(len(out or [])) for out in runs[-1].get("data", {}).get("main", []))
                         for name, runs in (result.get("runData") or {}).items())
        error = ((result.get("error") or {}).get("message") or "")[:120]
        print(" | ".join([str(e["id"]), e["status"], e["startedAt"][:19], (e.get("stoppedAt") or "")[11:19],
                          error, result.get("lastNodeExecuted") or "", nodes]))


# ---------- Data Tables ----------

def table_rows(table_id):
    yield from paged(f"/data-tables/{table_id}/rows")


def cmd_tables():
    for t in paged("/data-tables"):
        cols = ", ".join(f"{c['name']}:{c['type']}" for c in t.get("columns", []))
        print(f"{t['id']}  {t['name']}  [{cols}]")


def cmd_ensure(spec):
    existing = {t["name"]: t for t in paged("/data-tables")}
    for table in json.loads(Path(spec).read_text()):
        current = existing.get(table["name"])
        if not current:
            api("POST", "/data-tables", table)
            print(f"✓ created {table['name']}")
            continue
        have = {c["name"] for c in current.get("columns", [])}
        for col in table["columns"]:
            if col["name"] not in have:
                api("POST", f"/data-tables/{current['id']}/columns", col)
                print(f"✓ column added: {table['name']}.{col['name']}")
        print(f"✓ up to date: {table['name']}")


def cmd_rows(ref):
    for row in table_rows(find_table(ref)["id"]):
        print(json.dumps(row, ensure_ascii=False))


def cmd_add_rows(ref, file, key, *flags):
    table = find_table(ref)
    rows = json.loads(Path(file).read_text())
    if not (isinstance(rows, list) and all(isinstance(r, dict) and str(r.get(key) or "").strip() for r in rows)):
        die(f"{file} must be a list of objects that all have a {key!r} value")
    unknown = {k for r in rows for k in r} - {c["name"] for c in table.get("columns", [])}
    if unknown:
        die(f"columns missing from {table['name']}: {', '.join(sorted(unknown))}")
    norm = lambda v: str(v or "").strip().lower()
    seen = {norm(r.get(key)) for r in table_rows(table["id"])}
    new = []
    for r in rows:                      # first occurrence only, never a key already in the table
        if norm(r[key]) not in seen:
            seen.add(norm(r[key]))
            new.append(r)
    for r in new:
        print(f"  + {r[key]}")
    skipped = len(rows) - len(new)
    if not new:
        print(f"✓ nothing to add ({skipped} already there)")
    elif not confirmed(flags):
        print(f"✓ dry run: {len(new)} rows would be added, {skipped} already there (add --yes)")
    else:
        out = api("POST", f"/data-tables/{table['id']}/rows", {"data": new, "returnType": "count"})
        print(f"✓ {len(new)} rows added ({skipped} already there): {json.dumps(out)}")


def by_id(row_id):
    return {"type": "and", "filters": [{"columnName": "id", "condition": "eq", "value": row_id}]}


def check_rows(table, ids):
    missing = set(ids) - {r["id"] for r in table_rows(table["id"])}
    if missing:
        die(f"rows not found in {table['name']}: {', '.join(map(str, sorted(missing)))}")


def cmd_update_rows(ref, ids, assignment, *flags):
    table = find_table(ref)
    column, sep, value = assignment.partition("=")
    if not sep or column not in {c["name"] for c in table.get("columns", [])}:
        die(f"expected <column>=<value> with a column of {table['name']}: {assignment}")
    ids = ids_arg(ids)
    check_rows(table, ids)
    if not confirmed(flags):
        return print(f"✓ dry run: {len(ids)} rows would get {column}={value} (add --yes)")
    for row_id in ids:
        out = api("PATCH", f"/data-tables/{table['id']}/rows/update",
                  {"filter": by_id(row_id), "data": {column: value}, "returnData": True})
        if not (isinstance(out, list) and len(out) == 1):
            die(f"row {row_id} did not match exactly one row: {out}")
    print(f"✓ {len(ids)} rows updated: {column}={value}")


def cmd_delete_rows(ref, ids, *flags):
    table = find_table(ref)
    ids = ids_arg(ids)
    check_rows(table, ids)
    if not confirmed(flags):
        return print(f"✓ dry run: {len(ids)} rows would be deleted (add --yes)")
    for row_id in ids:
        out = api("DELETE", f"/data-tables/{table['id']}/rows/delete",
                  query={"filter": json.dumps(by_id(row_id), separators=(",", ":")), "returnData": "true"})
        if not (isinstance(out, list) and len(out) == 1):
            die(f"row {row_id} did not match exactly one row: {out}")
    print(f"✓ {len(ids)} rows deleted")


COMMANDS = {
    "workflows": cmd_workflows, "deploy": cmd_deploy, "publish": cmd_publish,
    "unpublish": lambda ref: cmd_publish(ref, publish=False), "rename": cmd_rename, "remove": cmd_remove, "executions": cmd_executions,
    "tables": cmd_tables, "ensure": cmd_ensure, "rows": cmd_rows, "add-rows": cmd_add_rows,
    "update-rows": cmd_update_rows, "delete-rows": cmd_delete_rows,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    command = COMMANDS[sys.argv[1]]
    try:
        inspect.signature(command).bind(*sys.argv[2:])
    except TypeError:
        sys.exit(__doc__)
    command(*sys.argv[2:])
