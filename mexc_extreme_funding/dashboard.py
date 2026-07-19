from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from mexc_extreme_funding.config import DEFAULT_CONFIG, MexcExtremeFundingConfig
from mexc_extreme_funding.models import parse_float, utc_now
from mexc_extreme_funding.paper_store import PaperStore
from mexc_extreme_funding.scanner import load_latest_snapshots


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MEXC Extreme Funding</title><style>
:root{--bg:#f4f5f2;--panel:#fff;--ink:#171914;--muted:#697066;--line:#dfe3dc;--accent:#16734b;--warn:#a55f00;--bad:#b4362d}*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 Inter,Segoe UI,Arial,sans-serif;letter-spacing:0}header{background:#181b17;color:#fff;padding:20px 28px 14px;border-bottom:4px solid #2aa89b}header h1{margin:0;font-size:24px;font-weight:650}header p{margin:4px 0 0;color:#c8cec5}nav{display:flex;gap:4px;padding:12px 28px 0;background:#181b17;overflow:auto}nav button{border:0;border-bottom:3px solid transparent;background:transparent;color:#bdc4b9;padding:10px 14px;cursor:pointer;font-weight:600}nav button.active{color:#fff;border-color:#2aa89b}main{padding:18px 28px 36px;max-width:1600px;margin:auto}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.status{color:var(--muted)}button.refresh{border:1px solid var(--line);background:var(--panel);border-radius:4px;padding:8px 12px;cursor:pointer}.metrics{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:10px;margin-bottom:16px}.metric{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:13px 14px}.metric span{color:var(--muted);display:block;font-size:12px}.metric strong{display:block;font-size:21px;margin-top:4px}.table-wrap{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px}table{width:100%;border-collapse:collapse;min-width:960px}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #eceee9;white-space:nowrap}th{position:sticky;top:0;background:#f8f9f7;color:#565d53;font-size:12px;text-transform:uppercase}tr:last-child td{border-bottom:0}.good{color:var(--accent);font-weight:650}.warn{color:var(--warn);font-weight:650}.bad{color:var(--bad);font-weight:650}.empty{padding:30px;text-align:center;color:var(--muted)}@media(max-width:800px){header,nav,main{padding-left:14px;padding-right:14px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.toolbar{align-items:flex-start}}
</style></head><body>
<header><h1>MEXC Extreme Funding</h1><p>Independent live funding, basis and paper execution</p></header>
<nav><button class="tab active" data-tab="funding">Funding</button><button class="tab" data-tab="shortlist">Shortlist</button><button class="tab" data-tab="positions">Positions</button><button class="tab" data-tab="daily-pnl">Daily PnL</button><button class="tab" data-tab="summary">Summary</button></nav>
<main><div class="toolbar"><div id="status" class="status">Loading...</div><button id="refresh" class="refresh">Refresh</button></div><div id="metrics" class="metrics"></div><div class="table-wrap"><table><thead id="head"></thead><tbody id="body"></tbody></table></div></main>
<script>
const $=s=>document.querySelector(s),esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct=v=>v===null||v===""||v===undefined?"-":Number(v).toFixed(4)+"%",usd=v=>v===null||v===""||v===undefined?"-":Number(v).toLocaleString(undefined,{style:"currency",currency:"USD"}),dt=v=>v?new Date(v).toLocaleString():"-";let active="funding";
const columns={funding:[["perp_symbol","Contract"],["current_funding_rate_pct","Displayed"],["minutes_to_funding","Minutes"],["mark_index_basis_pct","Mark / index"],["executable_basis_pct","Spot / perp"],["spot_symbol","Spot"],["reason","State"]],shortlist:[["perp_symbol","Contract"],["direction","Direction"],["latest_rate_pct","Latest"],["min_abs_rate_pct","Min abs"],["max_abs_rate_pct","Max abs"],["streak_observations","Streak"],["streak_age_minutes","Streak age"],["funding_time_utc","Funding"],["status","State"]],positions:[["position_id","Position"],["perp_symbol","Contract"],["direction","Direction"],["layer_index","Layer"],["notional_usd","Notional"],["displayed_rate_at_entry_pct","Entry rate"],["actual_funding_rate_pct","Actual rate"],["entry_basis_pct","Entry basis"],["current_basis_pct","Current basis"],["basis_pnl_pct","Basis PnL"],["estimated_net_pnl_pct","Net PnL"],["management_state","Management"],["status","State"],["exit_reason","Exit"]],"daily-pnl":[["date_utc","UTC date"],["realised_pnl_usd","Realised PnL"],["exit_count","Exit events"],["exit_notional_usd","Exited notional"],["funding_accrued_usd","Funding accrued"],["funding_events","Funding events"]],summary:[["label","Measure"],["value","Value"]]};
function cls(v,k){if(k==="reason"||k==="status")return String(v).includes("eligible")||v==="ACTIVE"||v==="OPEN"?"good":"warn";if(["current_funding_rate_pct","latest_rate_pct","basis_pnl_pct","estimated_net_pnl_pct"].includes(k))return Number(v)>=0?"good":"bad";return""}function val(v,k){if(k.includes("rate")||k.includes("basis")||k.includes("pnl_pct"))return pct(v);if(k.includes("pnl_usd")||k==="notional_usd"||k==="exit_notional_usd"||k==="funding_accrued_usd")return usd(v);if(k==="date_utc")return v??"-";if(k.includes("_utc"))return dt(v);if(k==="minutes_to_funding")return v==null?"-":Number(v).toFixed(1);return v??"-"}
function render(p){const rows=p.items||[],cols=columns[active];$("#head").innerHTML="<tr>"+cols.map(c=>`<th>${esc(c[1])}</th>`).join("")+"</tr>";$("#body").innerHTML=rows.length?rows.map(r=>"<tr>"+cols.map(c=>`<td class="${cls(r[c[0]],c[0])}">${esc(val(r[c[0]],c[0]))}</td>`).join("")+"</tr>").join(""):`<tr><td class="empty" colspan="${cols.length}">No records yet</td></tr>`;$("#metrics").innerHTML=(p.metrics||[]).map(m=>`<div class="metric"><span>${esc(m.label)}</span><strong>${esc(m.value)}</strong></div>`).join("");$("#status").textContent=`Updated ${dt(p.observedAtUtc)}`}
async function load(){try{const r=await fetch(`/api/${active}`,{cache:"no-store"}),p=await r.json();if(!r.ok)throw Error(p.error||r.statusText);render(p)}catch(e){$("#status").textContent=e.message}}document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));b.classList.add("active");active=b.dataset.tab;load()});$("#refresh").onclick=load;setInterval(load,30000);load();
</script></body></html>"""


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _funding_payload(config: MexcExtremeFundingConfig) -> dict:
    items = [snapshot.to_csv_row() for snapshot in load_latest_snapshots(config)]
    items.sort(key=lambda row: abs(parse_float(row.get("current_funding_rate_pct"), 0.0) or 0.0), reverse=True)
    largest = max((abs(parse_float(row.get("current_funding_rate_pct"), 0.0) or 0.0) for row in items), default=0)
    wide = sum(
        abs(parse_float(row.get("current_funding_rate_pct"), 0.0) or 0.0) >= config.min_abs_funding_rate_pct
        for row in items
    )
    return {"observedAtUtc": utc_now().isoformat(), "items": items, "metrics": [
        {"label": "Contracts", "value": str(len(items))},
        {"label": "Above 0.50%", "value": str(wide)},
        {"label": "Executable", "value": str(sum(str(row.get("eligible")).lower() == "true" for row in items))},
        {"label": "Largest absolute", "value": f"{largest:.4f}%"},
    ]}


def _shortlist_payload(config: MexcExtremeFundingConfig) -> dict:
    items = list(PaperStore(config).load_signals().values())
    items.sort(key=lambda row: abs(parse_float(row.get("latest_rate_pct"), 0.0) or 0.0), reverse=True)
    return {"observedAtUtc": utc_now().isoformat(), "items": items, "metrics": [
        {"label": "Active signals", "value": str(sum(row.get("status") == "ACTIVE" for row in items))},
        {"label": "Tracked events", "value": str(len(items))},
        {"label": "Required observations", "value": str(config.min_consistent_observations)},
        {"label": "Minimum layer interval", "value": f"{config.min_layer_interval_minutes:.0f} min"},
    ]}


def _positions_payload(config: MexcExtremeFundingConfig) -> dict:
    positions = [position.to_csv_row() for position in PaperStore(config).load_positions()]
    positions.sort(key=lambda row: (row.get("status") != "OPEN", row.get("entry_at_utc", "")), reverse=False)
    open_rows = [row for row in positions if row.get("status") == "OPEN"]
    return {"observedAtUtc": utc_now().isoformat(), "items": positions, "metrics": [
        {"label": "Open positions", "value": str(len(open_rows))},
        {"label": "Open notional", "value": f"${sum(parse_float(row.get('notional_usd'), 0.0) or 0.0 for row in open_rows):,.0f}"},
        {"label": "Estimated open PnL", "value": f"${sum((parse_float(row.get('estimated_net_pnl_pct'), 0.0) or 0.0) * (parse_float(row.get('notional_usd'), 0.0) or 0.0) / 100 for row in open_rows):,.2f}"},
        {"label": "Realised PnL", "value": f"${sum(parse_float(row.get('realised_pnl_usd'), 0.0) or 0.0 for row in positions):,.2f}"},
    ]}


def _daily_pnl_payload(config: MexcExtremeFundingConfig) -> dict:
    store = PaperStore(config)
    days: dict[str, dict] = {}

    def day_for(row: dict) -> dict:
        day = str(row.get("timestamp_utc", ""))[:10] or "unknown"
        return days.setdefault(day, {
            "date_utc": day, "realised_pnl_usd": 0.0, "exit_count": 0,
            "exit_notional_usd": 0.0, "funding_accrued_usd": 0.0, "funding_events": 0,
        })

    for row in store.read_rows(store.fills_path):
        if row.get("event_type") not in {"EXIT", "PARTIAL_EXIT"}:
            continue
        day = day_for(row)
        day["realised_pnl_usd"] += parse_float(row.get("realised_pnl_usd"), 0.0) or 0.0
        day["exit_count"] += 1
        day["exit_notional_usd"] += parse_float(row.get("notional_usd"), 0.0) or 0.0
    for row in store.read_rows(store.funding_events_path):
        day = day_for(row)
        day["funding_accrued_usd"] += parse_float(row.get("funding_pnl_usd"), 0.0) or 0.0
        day["funding_events"] += 1

    items = sorted(days.values(), key=lambda row: row["date_utc"], reverse=True)
    today = utc_now().date().isoformat()
    today_row = next((row for row in items if row["date_utc"] == today), None)
    realised_total = sum(row["realised_pnl_usd"] for row in items)
    return {"observedAtUtc": utc_now().isoformat(), "items": items, "metrics": [
        {"label": "Today realised PnL", "value": f"${(today_row or {}).get('realised_pnl_usd', 0.0):,.2f}"},
        {"label": "Today exit events", "value": str((today_row or {}).get("exit_count", 0))},
        {"label": "Today funding accrued", "value": f"${(today_row or {}).get('funding_accrued_usd', 0.0):,.2f}"},
        {"label": "All realised PnL", "value": f"${realised_total:,.2f}"},
    ]}


def _summary_payload(config: MexcExtremeFundingConfig) -> dict:
    comparisons = _read(config.data_dir / "settlement_comparisons.csv")
    events = {row.get("event_key", "") for row in comparisons}
    same = sum(str(row.get("same_direction", "")).lower() == "true" for row in comparisons)
    mean_error = sum(parse_float(row.get("absolute_error_pct"), 0.0) or 0.0 for row in comparisons) / len(comparisons) if comparisons else 0.0
    reasons = Counter(row.get("reason", "") for row in _read(PaperStore(config).decisions_path))
    items = [
        {"label": "Settled extreme events", "value": len(events)}, {"label": "Compared observations", "value": len(comparisons)},
        {"label": "Displayed direction held", "value": f"{(same / len(comparisons) * 100 if comparisons else 0):.1f}%"},
        {"label": "Mean displayed error", "value": f"{mean_error:.4f}%"}, {"label": "Basis take profit", "value": f"{config.basis_take_profit_pct:.2f}%"},
        {"label": "Adverse-basis stop", "value": "Disabled; hold and layer"},
        {"label": "Layer ladder", "value": " / ".join(f"${value:,.0f}" for value in config.layer_ladder_usd)},
        {"label": "Layer windows", "value": "120 / 60 / 30 / 12 minutes"},
        {"label": "Negative funding", "value": "Margin-enabled or inventory-backed spot only"},
        {"label": "Most common decision", "value": reasons.most_common(1)[0][0] if reasons else "-"},
    ]
    return {"observedAtUtc": utc_now().isoformat(), "items": items, "metrics": [
        {"label": "Events", "value": str(len(events))}, {"label": "Direction held", "value": f"{(same / len(comparisons) * 100 if comparisons else 0):.1f}%"},
        {"label": "Mean error", "value": f"{mean_error:.4f}%"},
    ]}


class DashboardHandler(BaseHTTPRequestHandler):
    config = DEFAULT_CONFIG

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, HTML.encode(), "text/html; charset=utf-8")
            return
        loaders = {"/api/funding": _funding_payload, "/api/shortlist": _shortlist_payload, "/api/positions": _positions_payload, "/api/daily-pnl": _daily_pnl_payload, "/api/summary": _summary_payload}
        loader = loaders.get(path)
        if loader is None:
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
            return
        try:
            self._send(HTTPStatus.OK, json.dumps(loader(self.config)).encode(), "application/json")
        except Exception as error:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, json.dumps({"error": str(error)}).encode(), "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        print(format % args, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MEXC extreme-funding dashboard.")
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8771); args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"MEXC extreme-funding dashboard at http://{args.host}:{args.port}/", flush=True); server.serve_forever()


if __name__ == "__main__":
    main()
