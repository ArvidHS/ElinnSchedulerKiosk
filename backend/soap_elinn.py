import os
from datetime import datetime
from dateutil import tz
import requests
from lxml import etree

from zeep import Client
from zeep.transports import Transport
from zeep.plugins import HistoryPlugin

WSDL = "https://api.elinn.no/webservice/order.php?WSDL"
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
ELINN_NS = "http://api.elinn.no/webservice/"


def _to_iso(dt_str: str, tz_name: str) -> str:
    """
    Konverterer "YYYY-mm-dd HH:MM:SS" (lokal tid) til ISO-8601 med timezone.
    """
    local_tz = tz.gettz(tz_name)
    naive = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    aware = naive.replace(tzinfo=local_tz)
    return aware.isoformat()


def _text(el) -> str | None:
    if el is None:
        return None
    t = el.text
    if t is None:
        return None
    t = t.strip()
    return t if t != "" else None


class ELinnClient:
    def __init__(self):
        self.username = os.environ.get("ELINN_USERNAME", "")
        self.password = os.environ.get("ELINN_PASSWORD", "")
        self.system = os.environ.get("ELINN_SYSTEM", "")
        self.tz_name = os.environ.get("TZ_NAME", "Europe/Oslo")

        if not self.username or not self.password:
            raise RuntimeError("Mangler ELINN_USERNAME / ELINN_PASSWORD")

        session = requests.Session()
        transport = Transport(session=session, timeout=30)

        # Viktig: vi bruker HistoryPlugin for å hente ut rå XML-respons
        self.history = HistoryPlugin()
        self.client = Client(wsdl=WSDL, transport=transport, plugins=[self.history])

        # SOAP-typer for filters (matcher din WSDL-signatur)
        self._FilterData = self.client.get_type("{http://api.elinn.no/webservice/}filterData")
        self._FilterLines = self.client.get_type("{http://api.elinn.no/webservice/}filterLines")

    def _parse_export_response(self):
        """
        Leser siste SOAP-respons fra HistoryPlugin og parser:
        - success/message/error/nextoffset
        - exportedEvents/exportedEvent
        Tåler at 'success' er 200 (ja, seriøst).
        """
        env = self.history.last_received["envelope"]
        body = env.find(f"{{{SOAP_ENV_NS}}}Body")
        if body is None:
            raise RuntimeError("Fant ingen SOAP Body i responsen")

        # Finn exportSchedulerEventResult uansett namespaces
        result_nodes = body.xpath('//*[local-name()="exportSchedulerEventResult"]')
        if not result_nodes:
            fault_nodes = body.xpath('//*[local-name()="Fault"]')
            if fault_nodes:
                raise RuntimeError(f"SOAP Fault: {etree.tostring(fault_nodes[0], encoding='unicode')}")
            raise RuntimeError("Fant ingen exportSchedulerEventResult i responsen")

        result = result_nodes[0]

        def find_text(name: str) -> str | None:
            nodes = result.xpath(f'.//*[local-name()="{name}"]')
            if not nodes:
                return None
            t = nodes[0].text
            if t is None:
                return None
            t = t.strip()
            return t if t else None

        success = find_text("success")
        error = find_text("error")
        message = find_text("message")
        nextoffset = find_text("nextoffset")

        # error: hvis den finnes og ikke er 0, er det feil
        if error and error not in ("0", 0):
            raise RuntimeError(f"ELinn error={error} message={message}")

        # success: godta flere varianter (1/true/OK/200)
        if success is not None:
            s = str(success).strip().lower()
            ok_values = {"1", "true", "ok", "200"}
            if s not in ok_values:
                raise RuntimeError(f"ELinn success={success} message={message}")

        # Finn events: exportedEvents/exportedEvent (tåler namespaces)
        event_nodes = result.xpath(
            './/*[local-name()="exportedEvents"]//*[local-name()="exportedEvent"]'
        )

        events = []
        for ev in event_nodes:
            def ev_text(field: str) -> str | None:
                n = ev.xpath(f'.//*[local-name()="{field}"]')
                if not n:
                    return None
                t = n[0].text
                if t is None:
                    return None
                t = t.strip()
                return t if t else None

            ev_id = ev_text("id")
            if not ev_id:
                continue

            events.append({
                "id": int(ev_id),
                "title": ev_text("title"),
                "dateStart": ev_text("dateStart"),
                "dateEnd": ev_text("dateEnd"),
                "typeId": ev_text("typeId"),
                "typeName": ev_text("typeName"),
                "comment": ev_text("comment"),
                "description": ev_text("description"),
                "updatedAt": ev_text("updatedAt"),
                "color": ev_text("color"),
                "userId": ev_text("userId"),
                "userIdExt": ev_text("userIdExt"),
            })


        return {
            "message": message,
            "nextoffset": nextoffset,
            "events": events,
        }


    def export_events_updated_after(self, updated_after: str | None):
        """
        Henter scheduler-events oppdatert etter tidspunktet.
        updated_after-format: "YYYY-mm-dd HH:MM:SS"
        Returnerer en liste med dicts (ikke zeep-objekter), basert på XML-parsing.
        """
        offset = 0
        all_events = []

        if not updated_after:
            updated_after = "1970-01-01 00:00:00"

        filter_item = self._FilterData(property="updated", operand="gt", value=updated_after)
        filters_payload = self._FilterLines(filter=[filter_item])

        while True:
            # Vi bryr oss ikke om returverdien fra zeep her, fordi vi parser XML via HistoryPlugin
            self.client.service.exportSchedulerEvent(
                username=self.username,
                password=self.password,
                system=self.system,
                offset=offset,
                filters=filters_payload,
            )

            parsed = self._parse_export_response()

            msg = parsed.get("message")
            if msg:
                print(f"[sync] ELinn message: {msg}")

            batch = parsed.get("events", [])
            all_events.extend(batch)

            nextoffset = parsed.get("nextoffset")
            if not nextoffset:
                break

            try:
                offset = int(nextoffset)
            except ValueError:
                break

        return all_events

    def map_event(self, ev) -> dict:
        """
        Mapper event til internt format (FullCalendar/DB).
        Støtter både dict (fra XML parsing) og zeep-objekter.
        """
        def get(name, default=None):
            if isinstance(ev, dict):
                return ev.get(name, default)
            return getattr(ev, name, default)

        date_start = get("dateStart")
        date_end = get("dateEnd")

        start_iso = _to_iso(str(date_start), self.tz_name) if date_start else None
        end_iso = _to_iso(str(date_end), self.tz_name) if date_end else None

        updated_at = get("updatedAt")
        color = get("cssColor") or get("color")

        type_id_raw = get("typeId") or 0
        try:
            type_id = int(type_id_raw)
        except Exception:
            type_id = 0

        title = get("title") or get("typeName") or f"Event {get('id')}"

        return {
            "id": int(get("id")),
            "title": str(title),
            "start": start_iso,
            "end": end_iso,
            "type_id": type_id,
            "type_name": str(get("typeName")) if get("typeName") else None,
            "color": str(color) if color else None,
            "comment": str(get("comment")) if get("comment") else None,
            "descr": str(get("description")) if get("description") else None,
            "updated_at": str(updated_at) if updated_at else None,
            "user_id": str(get("userId")) if get("userId") else None,
            "user_name": str(get("userIdExt")) if get("userIdExt") else None,

        }
