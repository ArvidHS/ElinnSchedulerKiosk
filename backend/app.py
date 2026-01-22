import os
import time
import threading
from datetime import datetime

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from db import init_db, get_meta, set_meta, upsert_event, query_events, get_conn
from soap_elinn import ELinnClient

app = FastAPI(title="Infoskjerm Kalender API")

# La frontend på 8080 hente fra backend på 8000
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # for kiosk er dette greit, ellers stram inn
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "60"))
LAST_SYNC_KEY = "last_sync_updated_gt"

def _now_str_for_elinn() -> str:
    # ELinn bruker "Y-m-d H:i:s"
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def sync_loop():
    # Vent litt så appen kommer opp
    time.sleep(2)

    client = ELinnClient()

    while True:
        try:
            last_sync = get_meta(LAST_SYNC_KEY)
            events = client.export_events_updated_after(last_sync)
            # Oppdater DB
            newest_updated = last_sync
            for ev in events:
                mapped = client.map_event(ev)
                if not mapped.get("start") or not mapped.get("end"):
                    continue
                upsert_event(mapped)
                # Oppdater newest_updated basert på updated_at (string sammenligning funker hvis format er likt)
                ua = mapped.get("updated_at")
                if ua and (newest_updated is None or ua > newest_updated):
                    newest_updated = ua

            # Hvis vi fikk noe, flytt sync-vinduet
            if newest_updated:
                set_meta(LAST_SYNC_KEY, newest_updated)
            else:
                # Første gang, hvis tomt, sett et utgangspunkt så vi ikke alltid henter "alt"
                if last_sync is None:
                    set_meta(LAST_SYNC_KEY, "1970-01-01 00:00:00")

            print(f"[sync] ok. fetched={len(events)} last_sync={get_meta(LAST_SYNC_KEY)}")

        except Exception as e:
            print(f"[sync] ERROR: {e}")

        time.sleep(SYNC_INTERVAL)

@app.on_event("startup")
def on_startup():
    init_db()
    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/events")
def api_events(
    start: str = Query(..., description="ISO timestamp, f.eks. 2026-01-20T00:00:00+01:00"),
    end: str = Query(..., description="ISO timestamp"),
):
    # Returner i FullCalendar-format
    rows = query_events(start, end)
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"] or f"Event {r['id']}",
            "start": r["start"],
            "end": r["end"],
            "resourceId": (str(r["user_id"]) if r.get("user_id") else "ukjent"),
            "backgroundColor": r["color"],
            "borderColor": r["color"],
            "extendedProps": {
                "typeId": r["type_id"],
                "typeName": r["type_name"],
                "comment": r["comment"],
                "descr": r["descr"],
                "updatedAt": r["updated_at"],
                "userName": r.get("user_name"),
            },
        })
    return out

import json
from pathlib import Path
from db import get_conn

USER_MAP_PATH = Path("/config/user_map.json")


def load_user_map() -> dict:
    if USER_MAP_PATH.exists():
        try:
            return json.loads(USER_MAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

@app.get("/api/resources")
def api_resources():
    user_map = load_user_map()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT
              user_id AS id,
              MAX(NULLIF(user_name,'')) AS ansattnr
            FROM events
            WHERE user_id IS NOT NULL
              AND user_id != ''
              AND user_id != '1'
            GROUP BY user_id
            ORDER BY id
            """
        ).fetchall()

        out = []
        for r in rows:
            uid = str(r["id"])
            ansattnr = r["ansattnr"]
            name = user_map.get(uid)

            # bruk mapping hvis den finnes og ikke er tom/"NULL"
            if name and str(name).strip() and str(name).strip().upper() != "NULL":
                title = str(name).strip()
            else:
                title = str(ansattnr) if ansattnr else uid

            out.append({"id": uid, "title": title})
        return out
    finally:
        conn.close()

