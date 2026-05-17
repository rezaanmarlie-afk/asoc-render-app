
import os
import io
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

load_dotenv()

SMARTSHEET_TOKEN = os.getenv("SMARTSHEET_ACCESS_TOKEN", "").strip()
SHEET_ID = os.getenv("SMARTSHEET_SHEET_ID", os.getenv("SHEET_ID", "6033225274419076")).strip()
SHEET_LINK = os.getenv("SMARTSHEET_SHEET_LINK", "https://app.smartsheet.com/sheets/Xvm992gjVPMChMhPGRVx85Gr24jqH2g45pj5wCH1").strip()

# Dual-sheet model:
# SOURCE sheet = existing ASR / operational demand sheet.
# GOVERNANCE sheet = clean ASOC Demand Governance Register template.
GOVERNANCE_SHEET_ID = os.getenv("SMARTSHEET_GOVERNANCE_SHEET_ID", os.getenv("GOVERNANCE_SHEET_ID", "")).strip()
GOVERNANCE_SHEET_LINK = os.getenv("SMARTSHEET_GOVERNANCE_SHEET_LINK", "").strip()

BASE_URL = "https://api.smartsheet.com/2.0"

# Short in-memory cache for source Smartsheet reads.
# Render starts a fresh process on deploy, so this cache is safe and temporary.
SHEET_CACHE_TTL_SECONDS = int(os.getenv("SHEET_CACHE_TTL_SECONDS", "300"))
_SHEET_CACHE = {"ts": 0.0, "include": None, "sheet": None}

os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app = FastAPI(title="ASOC Demand Governance Control Tower", version="3.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

APP_BUILD_VERSION = "GOV-WORKBENCH-DETAILS-COMMENTS-REGISTER-WRITEBACK-2026-05-17"

@app.get("/api/version")
def api_version():
    return {"version": APP_BUILD_VERSION, "governance_model": "lean18", "expected_columns": [c["title"] for c in governance_template_columns()]}



def headers() -> Dict[str, str]:
    if not SMARTSHEET_TOKEN:
        raise HTTPException(status_code=500, detail="SMARTSHEET_ACCESS_TOKEN is not set in .env")
    return {"Authorization": f"Bearer {SMARTSHEET_TOKEN}", "Content-Type": "application/json"}


def smartsheet(method: str, path: str, **kwargs):
    url = f"{BASE_URL}{path}"

    last_error = None
    for attempt in range(1, 4):
        try:
            res = requests.request(method, url, headers=headers(), timeout=90, **kwargs)
        except requests.RequestException as exc:
            last_error = f"Could not connect to Smartsheet API: {exc}"
            if attempt < 3:
                time.sleep(1.5 * attempt)
                continue
            raise HTTPException(status_code=502, detail=last_error)

        if res.ok:
            return res.json() if res.text else {}

        # Smartsheet 4004 is often transient or caused by heavy sheet reads.
        # Retry a few times before surfacing the error.
        try:
            err_json = res.json()
            error_code = err_json.get("errorCode")
        except Exception:
            err_json = res.text
            error_code = None

        last_error = {"url": url, "error": res.text, "attempt": attempt}

        if error_code == 4004 and attempt < 3:
            time.sleep(1.5 * attempt)
            continue

        raise HTTPException(status_code=res.status_code, detail=last_error)

    raise HTTPException(status_code=502, detail=last_error or f"Unknown Smartsheet error calling {url}")


def get_sheet(include: str = "objectValue", use_cache: bool = True) -> Dict[str, Any]:
    """Read the source Smartsheet with a short cache for better UX.

    The Executive Dashboard now loads by criteria, and repeated filter/dashboard
    actions should not force a full SmartSheet read every time. Any write action
    calls invalidate_sheet_cache() so the next read is fresh.
    """
    now_ts = time.time()
    if use_cache and _SHEET_CACHE.get("sheet") is not None and _SHEET_CACHE.get("include") == include and now_ts - float(_SHEET_CACHE.get("ts") or 0) < SHEET_CACHE_TTL_SECONDS:
        return _SHEET_CACHE["sheet"]

    params = {"include": include} if include else {}
    try:
        sheet = smartsheet("GET", f"/sheets/{SHEET_ID}", params=params)
    except HTTPException as exc:
        # If heavy source sheet read fails with include=objectValue, retry without include.
        # Most of this app only needs displayValue/value, not objectValue.
        if include:
            try:
                sheet = smartsheet("GET", f"/sheets/{SHEET_ID}", params={})
            except HTTPException:
                raise exc
        else:
            raise

    if use_cache:
        _SHEET_CACHE.update({"ts": now_ts, "include": include, "sheet": sheet})
    return sheet


def invalidate_sheet_cache():
    _SHEET_CACHE.update({"ts": 0.0, "include": None, "sheet": None})


def column_maps(sheet: Dict[str, Any]) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    cols = sheet.get("columns", [])
    return {c["id"]: c for c in cols}, {c["title"].strip().lower(): c for c in cols}


def row_to_dict(row: Dict[str, Any], by_id: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    record = {"_row_id": row.get("id"), "_row_number": row.get("rowNumber"), "_created_at": row.get("createdAt"), "_modified_at": row.get("modifiedAt")}
    for cell in row.get("cells", []):
        col = by_id.get(cell.get("columnId"), {})
        title = col.get("title", str(cell.get("columnId")))
        record[title] = cell.get("displayValue", cell.get("value", ""))
    return record


def normalised_rows(sheet: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_id, _ = column_maps(sheet)
    return [row_to_dict(r, by_id) for r in sheet.get("rows", [])]


def infer_key_columns(columns: List[str]) -> Dict[str, Optional[str]]:
    def pick(candidates):
        for cand in candidates:
            for col in columns:
                if cand in col.lower():
                    return col
        return None
    return {
        "reference": pick(["asr numbers auto", "asr number auto", "reference", "demand id", "id", "number"]),
        "status": pick(["status", "state", "stage"]),
        "priority": pick(["priority", "severity", "urgency", "criticality"]),
        "assignee": pick(["assignee", "assigned", "owner", "resource", "responsible"]),
        "requestor": pick(["requestor", "requester", "submitted by", "raised by"]),
        "created": pick(["created", "submitted", "date raised", "request date", "created date"]),
        "modified": pick(["modified", "updated", "last update"]),
        "demand": pick(["demand", "request", "title", "summary", "name", "description"]),
        "due": pick(["due", "target", "deadline", "planned end", "end date"]),
        "portfolio": pick(["portfolio", "domain", "tribe", "area", "department"]),
        "initiative_status": pick(["initiative status", "initiative status (auto)", "initiative_state"]),
        "analysis": pick(["analysis", "analysis field", "demand analysis"]),
    }


def parse_date(value: Any) -> Optional[pd.Timestamp]:
    if value in [None, ""]:
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce", dayfirst=False)
        if pd.isna(ts):
            return None
        if ts.tzinfo is not None:
            ts = ts.tz_convert(None)
        return ts
    except Exception:
        return None


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        f = float(value)
        if pd.isna(f) or f == float("inf") or f == float("-inf"):
            return default
        return f
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(safe_float(value, float(default))))
    except Exception:
        return default


def is_closed(value: Any) -> bool:
    s = str(value or "").strip().lower()
    return s in {"closed", "complete", "completed", "done", "cancelled", "canceled", "rejected"}


def safe_age_days(value: Any) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    try:
        now = pd.Timestamp.now()
        if getattr(value, "tzinfo", None) is not None:
            value = value.tz_convert(None)
        days = (now - value).days
        if pd.isna(days):
            return None
        return max(int(days), 0)
    except Exception:
        return None


def age_bucket(days: Optional[int]) -> str:
    if days is None or pd.isna(days): return "Unknown"
    if days <= 7: return "0-7 days"
    if days <= 14: return "8-14 days"
    if days <= 30: return "15-30 days"
    if days <= 60: return "31-60 days"
    if days <= 90: return "61-90 days"
    return ">90 days"


def build_dataframe() -> Tuple[Dict[str, Any], pd.DataFrame, Dict[str, Optional[str]]]:
    sheet = get_sheet(include="objectValue")
    rows = normalised_rows(sheet)
    columns = [c["title"] for c in sheet.get("columns", [])]
    keys = infer_key_columns(columns)
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=columns + ["_row_id", "_row_number", "_created_at", "_modified_at"])
    now = pd.Timestamp.now()
    created_col = keys.get("created")
    status_col = keys.get("status")
    due_col = keys.get("due")
    df["_created_parsed"] = df[created_col].apply(parse_date) if created_col in df.columns else df.get("_created_at", pd.Series(dtype=str)).apply(parse_date)
    df["_age_days"] = df["_created_parsed"].apply(safe_age_days)
    df["_age_bucket"] = df["_age_days"].apply(age_bucket)
    df["_is_closed"] = df[status_col].apply(is_closed) if status_col in df.columns else False
    if due_col in df.columns:
        df["_due_parsed"] = df[due_col].apply(parse_date)
        df["_is_overdue"] = df.apply(lambda r: bool(pd.notna(r["_due_parsed"]) and r["_due_parsed"] < now and not r["_is_closed"]), axis=1)
    else:
        df["_is_overdue"] = False
    df["_health"] = df.apply(lambda r: "Closed" if r["_is_closed"] else ("Overdue" if r["_is_overdue"] else ("Aging" if (r["_age_days"] if pd.notna(r["_age_days"]) else 0) > 30 else "Healthy")), axis=1)
    return sheet, df, keys



def build_governance_dataframe() -> Tuple[Dict[str, Any], pd.DataFrame, Dict[str, Optional[str]]]:
    """Read Governance Register as its own dataframe.

    Governance Register is the committed discussion/outcome/action layer.
    This dataframe is later joined to source demands so dashboard criteria can
    filter both: logged demands and governance commitments.
    """
    if not GOVERNANCE_SHEET_ID:
        empty = pd.DataFrame()
        return {"columns": [], "rows": [], "name": "Governance Register not configured"}, empty, {}
    try:
        sheet = get_governance_sheet(include="objectValue")
    except Exception:
        empty = pd.DataFrame()
        return {"columns": [], "rows": [], "name": "Governance Register unavailable"}, empty, {}
    rows = normalised_rows(sheet)
    columns = [c["title"] for c in sheet.get("columns", [])]
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=columns + ["_row_id", "_row_number", "_created_at", "_modified_at"])
    keys = {
        "asr": "ASR Numbers Auto" if "ASR Numbers Auto" in df.columns else ("ASR Number" if "ASR Number" in df.columns else None),
        "source_row_id": "Source Row ID" if "Source Row ID" in df.columns else None,
        "target_pi": "Target PI" if "Target PI" in df.columns else None,
        "decision": "Governance Decision" if "Governance Decision" in df.columns else ("Stakeholder Decision" if "Stakeholder Decision" in df.columns else None),
        "action_required": "Action Required" if "Action Required" in df.columns else ("Next Action" if "Next Action" in df.columns else None),
        "action_owner": "Action Owner" if "Action Owner" in df.columns else None,
        "action_due": "Action Due Date" if "Action Due Date" in df.columns else None,
        "readiness": "Demand Readiness" if "Demand Readiness" in df.columns else None,
        "last_update": "Last RTE Update" if "Last RTE Update" in df.columns else None,
    }
    return sheet, df, keys


def _normalise_join_key(value: Any) -> str:
    return str(value or "").strip().upper()


def build_governance_dashboard_dataframe() -> Tuple[Dict[str, Any], pd.DataFrame, Dict[str, Any]]:
    """Combine source demand rows with Governance Register rows for dashboarding.

    Output keeps normal demand columns as-is and prefixes register columns with
    'Governance - '. This makes the dashboard criteria explicit and avoids
    confusion about whether a filter is against demand intake data or committed
    governance outcomes/actions.
    """
    source_sheet, source_df, source_keys = build_dataframe()
    gov_sheet, gov_df, gov_keys = build_governance_dataframe()
    merged = source_df.copy()

    # Build source join keys.
    ref_col = source_keys.get("reference")
    if ref_col in merged.columns:
        merged["_join_asr"] = merged[ref_col].apply(_normalise_join_key)
    else:
        merged["_join_asr"] = ""
    merged["_join_source_row_id"] = merged.get("_row_id", pd.Series(dtype=str)).apply(_normalise_join_key)

    if not gov_df.empty:
        gov = gov_df.copy()
        gov_asr_col = gov_keys.get("asr")
        gov_row_id_col = gov_keys.get("source_row_id")
        gov["_join_asr"] = gov[gov_asr_col].apply(_normalise_join_key) if gov_asr_col in gov.columns else ""
        gov["_join_source_row_id"] = gov[gov_row_id_col].apply(_normalise_join_key) if gov_row_id_col in gov.columns else ""

        # De-duplicate register to latest per ASR/source row so each demand appears once.
        sort_col = "_modified_at" if "_modified_at" in gov.columns else "_row_number"
        gov = gov.sort_values(sort_col).copy()
        gov["_gov_match_key"] = gov["_join_asr"].where(gov["_join_asr"].ne(""), gov["_join_source_row_id"])
        gov = gov[gov["_gov_match_key"].astype(str).ne("")]
        gov = gov.drop_duplicates("_gov_match_key", keep="last")

        # Prefix Governance Register columns.
        keep_cols = [c for c in gov.columns if not c.endswith("_parsed")]
        gov_pref = gov[keep_cols].rename(columns={c: f"Governance - {c}" for c in keep_cols if not c.startswith("_join") and c != "_gov_match_key"})
        gov_pref["_gov_match_key"] = gov["_gov_match_key"]

        merged["_gov_match_key"] = merged["_join_asr"].where(merged["_join_asr"].ne(""), merged["_join_source_row_id"])
        merged = merged.merge(gov_pref, how="left", on="_gov_match_key")
    else:
        merged["_gov_match_key"] = merged["_join_asr"].where(merged["_join_asr"].ne(""), merged["_join_source_row_id"])

    # Governance-derived flags for dashboard filtering and metrics.
    gov_decision_col = "Governance - Governance Decision" if "Governance - Governance Decision" in merged.columns else "Governance - Stakeholder Decision"
    gov_action_col = "Governance - Action Required" if "Governance - Action Required" in merged.columns else "Governance - Next Action"
    gov_action_owner_col = "Governance - Action Owner"
    gov_target_pi_col = "Governance - Target PI"
    gov_readiness_col = "Governance - Demand Readiness"
    gov_last_update_col = "Governance - Last RTE Update"
    gov_due_col = "Governance - Action Due Date"

    merged["_has_governance_commitment"] = False
    for c in [gov_decision_col, gov_action_col, gov_target_pi_col, gov_last_update_col]:
        if c in merged.columns:
            merged["_has_governance_commitment"] = merged["_has_governance_commitment"] | merged[c].fillna("").astype(str).str.strip().ne("")
    if gov_action_col in merged.columns:
        merged["_has_open_action"] = merged[gov_action_col].fillna("").astype(str).str.strip().ne("")
    else:
        merged["_has_open_action"] = False
    if gov_due_col in merged.columns:
        due_dates = merged[gov_due_col].apply(parse_date)
        merged["_governance_action_overdue"] = due_dates.apply(lambda d: bool(d is not None and d < pd.Timestamp.now())) & merged["_has_open_action"]
    else:
        merged["_governance_action_overdue"] = False

    combined_keys: Dict[str, Any] = {
        **source_keys,
        "governance": gov_keys,
        "gov_target_pi": gov_target_pi_col if gov_target_pi_col in merged.columns else None,
        "gov_decision": gov_decision_col if gov_decision_col in merged.columns else None,
        "gov_action_required": gov_action_col if gov_action_col in merged.columns else None,
        "gov_action_owner": gov_action_owner_col if gov_action_owner_col in merged.columns else None,
        "gov_action_due": gov_due_col if gov_due_col in merged.columns else None,
        "gov_readiness": gov_readiness_col if gov_readiness_col in merged.columns else None,
        "gov_last_update": gov_last_update_col if gov_last_update_col in merged.columns else None,
    }
    return source_sheet, merged, combined_keys


def governance_dashboard_insights(df: pd.DataFrame, keys: Dict[str, Any]) -> Dict[str, Any]:
    total = int(len(df))
    committed = int(df.get("_has_governance_commitment", pd.Series([False]*len(df))).sum()) if total else 0
    open_actions = int(df.get("_has_open_action", pd.Series([False]*len(df))).sum()) if total else 0
    overdue_actions = int(df.get("_governance_action_overdue", pd.Series([False]*len(df))).sum()) if total else 0
    target_pi_col = keys.get("gov_target_pi")
    action_owner_col = keys.get("gov_action_owner")
    decision_col = keys.get("gov_decision")
    readiness_col = keys.get("gov_readiness")
    action_col = keys.get("gov_action_required")
    due_col = keys.get("gov_action_due")
    ref_col = keys.get("reference")
    demand_col = keys.get("demand")

    action_rows = []
    if total and action_col in df.columns:
        actions_df = df[df[action_col].fillna("").astype(str).str.strip().ne("")].copy()
        if due_col in actions_df.columns:
            actions_df["_action_due_sort"] = actions_df[due_col].apply(parse_date)
            actions_df = actions_df.sort_values(["_governance_action_overdue", "_action_due_sort"], ascending=[False, True], na_position="last")
        for _, r in actions_df.head(50).iterrows():
            action_rows.append({
                "row_id": r.get("_row_id"),
                "asr": r.get(ref_col, "") if ref_col else "",
                "demand": r.get(demand_col, "") if demand_col else "",
                "target_pi": r.get(target_pi_col, "") if target_pi_col else "",
                "decision": r.get(decision_col, "") if decision_col else "",
                "action_required": r.get(action_col, "") if action_col else "",
                "action_owner": r.get(action_owner_col, "") if action_owner_col else "",
                "action_due": r.get(due_col, "") if due_col else "",
                "overdue": bool(r.get("_governance_action_overdue", False)),
            })

    return {
        "metrics": {
            "governance_committed": committed,
            "governance_not_discussed": max(total - committed, 0),
            "governance_open_actions": open_actions,
            "governance_overdue_actions": overdue_actions,
        },
        "charts": {
            "governance_by_pi": series_counts(df, target_pi_col),
            "governance_action_owner": series_counts(df[df.get("_has_open_action", False)] if total else df, action_owner_col),
            "governance_decision": series_counts(df, decision_col),
            "governance_readiness": series_counts(df, readiness_col),
        },
        "actions": action_rows,
    }

def series_counts(df: pd.DataFrame, col: Optional[str], limit: int = 20) -> List[Dict[str, Any]]:
    if not col or col not in df.columns: return []
    s = df[col].fillna("Blank").replace("", "Blank").astype(str)
    return [{"name": k, "value": int(v)} for k, v in s.value_counts().head(limit).items()]


def clean_public_df(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in df.columns if c.endswith("_parsed")], errors="ignore").where(pd.notnull(df), "")


class DemandPayload(BaseModel):
    values: Dict[str, Any]
    to_top: bool = True


class UpdatePayload(BaseModel):
    values: Dict[str, Any]


class CommentPayload(BaseModel):
    text: str


class FilterRule(BaseModel):
    column: str
    operator: str
    value: Any = None


class FilterPayload(BaseModel):
    rules: List[FilterRule] = []
    logic: str = "AND"
    limit: int = 500


class GovernancePayload(BaseModel):
    business_outcome: str = ""
    urgency_reason: str = ""
    discussion_summary: str = ""
    meeting_attendees: str = ""
    scope_clear: bool = False
    scope_clear_detail: str = ""
    dependencies_known: bool = False
    dependencies_known_detail: str = ""
    data_available: bool = False
    data_available_detail: str = ""
    api_ready: bool = False
    api_ready_detail: str = ""
    nfr_defined: bool = False
    nfr_defined_detail: str = ""
    business_value: int = 5
    time_criticality: int = 5
    risk_reduction: int = 5
    job_size: int = 5
    capacity_impact: str = "Medium"
    stakeholder_decision: str = "Refine"
    next_action: str = ""
    action_owner: str = ""
    target_pi: str = ""
    recommendation_override: str = ""


def dashboard_from_df(sheet: Dict[str, Any], df: pd.DataFrame, keys: Dict[str, Optional[str]]) -> Dict[str, Any]:
    total = int(len(df))
    open_df = df[~df["_is_closed"]] if total and "_is_closed" in df.columns else df
    closed_df = df[df["_is_closed"]] if total and "_is_closed" in df.columns else df.iloc[0:0]
    assignee_col = keys.get("assignee")
    metrics = {
        "total_demands": total,
        "open_demands": int(len(open_df)),
        "closed_demands": int(len(closed_df)),
        "overdue_demands": int(df["_is_overdue"].sum()) if total and "_is_overdue" in df.columns else 0,
        "aging_over_30": int(((df["_age_days"].fillna(0) > 30) & (~df["_is_closed"])).sum()) if total and "_age_days" in df.columns and "_is_closed" in df.columns else 0,
        "avg_age_open": round(float(open_df["_age_days"].dropna().mean()), 1) if len(open_df) and "_age_days" in open_df.columns and not open_df["_age_days"].dropna().empty else 0,
        "unique_assignees": int(df[assignee_col].replace("", pd.NA).dropna().nunique()) if assignee_col in df.columns else 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    charts = {
        "status": series_counts(df, keys.get("status")),
        "priority": series_counts(df, keys.get("priority")),
        "assignee": series_counts(open_df, assignee_col),
        "age_bucket": series_counts(df, "_age_bucket"),
        "health": series_counts(df, "_health"),
        "portfolio": series_counts(df, keys.get("portfolio")),
    }
    risk_items = []
    visible = [c for c in [keys.get("reference"), keys.get("demand"), keys.get("status"), keys.get("priority"), keys.get("assignee"), keys.get("created"), keys.get("due")] if c]
    risk_df = df[(~df["_is_closed"])] if len(df) and "_is_closed" in df.columns else df
    if len(risk_df):
        risk_df = risk_df.sort_values(by=["_is_overdue", "_age_days"], ascending=[False, False]).head(15)
    for _, r in risk_df.iterrows():
        risk_items.append({"row_id": r.get("_row_id"), "row_number": r.get("_row_number"), "health": r.get("_health"), "age_days": r.get("_age_days") or "", "fields": {c: r.get(c, "") for c in visible}})
    return {"sheet_name": sheet.get("name"), "key_columns": keys, "metrics": metrics, "charts": charts, "risk_items": risk_items}


def apply_filter_rules(df: pd.DataFrame, payload: FilterPayload) -> pd.DataFrame:
    if df.empty or not payload.rules:
        return df.copy()
    masks = []
    for rule in payload.rules:
        col = rule.column
        op = (rule.operator or "contains").lower().strip()
        val = "" if rule.value is None else str(rule.value).strip()
        if col not in df.columns:
            continue
        series = df[col]
        text = series.fillna("").astype(str)
        low = text.str.lower()
        vlow = val.lower()
        if op == "contains":
            mask = low.str.contains(re.escape(vlow), na=False)
        elif op == "not_contains":
            mask = ~low.str.contains(re.escape(vlow), na=False)
        elif op == "equals":
            mask = low == vlow
        elif op == "not_equals":
            mask = low != vlow
        elif op == "starts_with":
            mask = low.str.startswith(vlow, na=False)
        elif op == "ends_with":
            mask = low.str.endswith(vlow, na=False)
        elif op == "blank":
            mask = text.str.strip().eq("") | series.isna()
        elif op == "not_blank":
            mask = text.str.strip().ne("") & series.notna()
        elif op in {"greater_than", "less_than", "greater_or_equal", "less_or_equal"}:
            numeric = pd.to_numeric(series, errors="coerce")
            try:
                number = float(val)
            except Exception:
                number = float("nan")
            if op == "greater_than": mask = numeric > number
            elif op == "less_than": mask = numeric < number
            elif op == "greater_or_equal": mask = numeric >= number
            else: mask = numeric <= number
        elif op in {"date_after", "date_before", "date_on"}:
            dates = series.apply(parse_date)
            target = parse_date(val)
            if target is None:
                mask = pd.Series([False] * len(df), index=df.index)
            elif op == "date_after": mask = dates > target
            elif op == "date_before": mask = dates < target
            else: mask = dates.apply(lambda x: x.date() == target.date() if x is not None else False)
        else:
            mask = low.str.contains(re.escape(vlow), na=False)
        masks.append(mask.fillna(False))
    if not masks:
        return df.copy()
    combined = masks[0]
    if (payload.logic or "AND").upper() == "OR":
        for m in masks[1:]: combined = combined | m
    else:
        for m in masks[1:]: combined = combined & m
    return df[combined].copy()


def _first_existing(keys: Dict[str, Optional[str]], df: pd.DataFrame, names: List[str]) -> Optional[str]:
    for k in names:
        if k in keys and keys.get(k) in df.columns:
            return keys.get(k)
    lowered = {c.lower(): c for c in df.columns}
    hints = {
        "business_owner": ["business owner", "business", "sponsor", "product owner", "requestor", "requester", "owner"],
        "description": ["description", "requirement", "scope", "summary", "demand", "request", "detail"],
        "outcome": ["benefit", "outcome", "value", "reason", "objective", "business case"],
        "impacted_system": ["impacted system", "system", "application", "platform", "uim", "jira", "domain", "component"],
        "dependency": ["dependency", "dependencies", "blocker", "blocked"],
        "data_availability": ["data availability", "data", "source", "dataset", "evidence"],
        "assignee": ["assignee", "assigned", "resource", "responsible"],
        "due": ["due", "deadline", "target", "planned end", "end date"],
        "effort": ["effort", "estimate", "story point", "points", "size", "complexity"],
        "priority": ["priority", "severity", "urgency", "criticality"],
        "security": ["security", "risk", "compliance", "spda", "privacy"],
        "data_classification": ["classification", "confidential", "pii", "personal", "sensitive"],
        "ai_impact": ["ai", "automation", "model", "genai", "ml"],
    }
    for name in names:
        for hint in hints.get(name, [name]):
            for lc, col in lowered.items():
                if hint in lc:
                    return col
    return None


def _has_value(row: pd.Series, col: Optional[str]) -> bool:
    if not col or col not in row.index:
        return False
    val = row.get(col, "")
    return not (val is None or pd.isna(val) or str(val).strip() == "")


def _text_value(row: pd.Series, col: Optional[str]) -> str:
    if not col or col not in row.index:
        return ""
    val = row.get(col, "")
    if val is None or pd.isna(val):
        return ""
    return str(val).strip()


def _priority_weight(priority: Any) -> float:
    s = str(priority or "").strip().lower()
    if any(x in s for x in ["critical", "urgent", "p1", "1", "high"]): return 1.5
    if any(x in s for x in ["medium", "p2", "2"]): return 1.15
    if any(x in s for x in ["low", "p3", "3", "minor"]): return 0.8
    return 1.0


def _score_category(row: pd.Series, checks: List[Tuple[str, Optional[str], int]]) -> Tuple[int, List[str]]:
    score = 0; missing = []
    for label, col, points in checks:
        if _has_value(row, col): score += points
        else: missing.append(label)
    return score, missing


def quality_columns(keys: Dict[str, Optional[str]], df: pd.DataFrame) -> Dict[str, Optional[str]]:
    return {
        "business_owner": _first_existing(keys, df, ["requestor", "business_owner"]),
        "description": _first_existing(keys, df, ["demand", "description"]),
        "outcome": _first_existing(keys, df, ["outcome"]),
        "impacted_system": _first_existing(keys, df, ["impacted_system", "portfolio"]),
        "dependency": _first_existing(keys, df, ["dependency"]),
        "data_availability": _first_existing(keys, df, ["data_availability"]),
        "assignee": _first_existing(keys, df, ["assignee"]),
        "due": _first_existing(keys, df, ["due"]),
        "effort": _first_existing(keys, df, ["effort"]),
        "priority": _first_existing(keys, df, ["priority"]),
        "security": _first_existing(keys, df, ["security"]),
        "data_classification": _first_existing(keys, df, ["data_classification"]),
        "ai_impact": _first_existing(keys, df, ["ai_impact"]),
    }


def score_one_demand(row: pd.Series, qcols: Dict[str, Optional[str]]) -> Dict[str, Any]:
    bc_score, bc_missing = _score_category(row, [("Business Owner / Requestor", qcols.get("business_owner"), 8),("Clear Description / Requirement", qcols.get("description"), 10),("Outcome / Benefit", qcols.get("outcome"), 7)])
    tr_score, tr_missing = _score_category(row, [("Impacted System / Domain", qcols.get("impacted_system"), 10),("Dependencies / Blockers", qcols.get("dependency"), 7),("Data Availability / Evidence", qcols.get("data_availability"), 8)])
    dr_score, dr_missing = _score_category(row, [("Assignee / Delivery Owner", qcols.get("assignee"), 8),("Due Date / Target Date", qcols.get("due"), 8),("Priority / Criticality", qcols.get("priority"), 5),("Effort / Size", qcols.get("effort"), 4)])
    gov_score, gov_missing = _score_category(row, [("Security / Compliance Considered", qcols.get("security"), 8),("Data Classification", qcols.get("data_classification"), 8),("AI / Automation Impact", qcols.get("ai_impact"), 9)])
    total = bc_score + tr_score + dr_score + gov_score
    missing = bc_missing + tr_missing + dr_missing + gov_missing
    status = "Ready" if total >= 80 else ("Needs Clarification" if total >= 50 else "Not Ready")
    confidence = "High" if total >= 80 and len(missing) <= 3 else ("Medium" if total >= 50 else "Low")
    reasons = []
    if bool(row.get("_is_overdue", False)): reasons.append("Overdue")
    if (row.get("_age_days") or 0) > 30 and not bool(row.get("_is_closed", False)): reasons.append("Aging >30 days")
    if missing: reasons.append("Missing: " + ", ".join(missing[:4]))
    return {"quality_score": int(total), "readiness": status, "confidence": confidence, "missing_fields": missing, "reason": "; ".join(reasons) if reasons else "Sufficient demand metadata for current stage.", "category_scores": {"Business Clarity": bc_score, "Technical Readiness": tr_score, "Delivery Readiness": dr_score, "Governance / AI Compliance": gov_score}}


def enrich_quality(df: pd.DataFrame, keys: Dict[str, Optional[str]]) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        out["_quality_score"] = []; out["_readiness"] = []; out["_confidence"] = []
        return out
    qcols = quality_columns(keys, out)
    scores = [score_one_demand(r, qcols) for _, r in out.iterrows()]
    out["_quality_score"] = [s["quality_score"] for s in scores]
    out["_readiness"] = [s["readiness"] for s in scores]
    out["_confidence"] = [s["confidence"] for s in scores]
    out["_missing_fields"] = [", ".join(s["missing_fields"][:8]) for s in scores]
    out["_quality_reason"] = [s["reason"] for s in scores]
    return out


def capacity_from_df(df: pd.DataFrame, keys: Dict[str, Optional[str]]) -> Dict[str, Any]:
    assignee_col = keys.get("assignee") if keys.get("assignee") in df.columns else None
    priority_col = keys.get("priority") if keys.get("priority") in df.columns else None
    if df.empty or not assignee_col:
        return {"team": [], "summary": {"team_load_percent": 0, "overloaded_resources": 0, "active_resources": 0}}

    open_df = df[~df["_is_closed"].fillna(False)].copy() if "_is_closed" in df.columns else df.copy()
    open_df["_assignee_norm"] = open_df[assignee_col].fillna("Unassigned").replace("", "Unassigned").astype(str)

    rows = []
    baseline_capacity = 10.0
    for name, g in open_df.groupby("_assignee_norm", dropna=False):
        active = int(len(g))
        overdue = int(g["_is_overdue"].fillna(False).sum()) if "_is_overdue" in g.columns else 0
        if "_age_days" in g.columns:
            clean_ages = [safe_float(v, 0.0) for v in g["_age_days"].tolist() if not pd.isna(v)]
            avg_age = round(sum(clean_ages) / len(clean_ages), 1) if clean_ages else 0
        else:
            avg_age = 0

        weighted = 0.0
        for _, r in g.iterrows():
            age_days = safe_float(r.get("_age_days"), 0.0)
            age_factor = 1.0 + min(max(age_days, 0.0), 90.0) / 180.0
            p_factor = _priority_weight(r.get(priority_col)) if priority_col else 1.0
            quality_score = safe_int(r.get("_quality_score", 100), 100)
            q_factor = 1.25 if quality_score < 50 else 1.0
            weighted += safe_float(p_factor, 1.0) * age_factor * q_factor

        weighted = safe_float(weighted, 0.0)
        load_pct = safe_int(min(round((weighted / baseline_capacity) * 100), 250), 0)
        risk = "High" if load_pct >= 100 or overdue >= 3 else ("Medium" if load_pct >= 75 or overdue >= 1 else "Low")
        rows.append({"assignee": str(name or "Unassigned"), "active": active, "overdue": overdue, "avg_age": avg_age, "weighted_load": round(weighted, 1), "load_percent": load_pct, "risk": risk})

    rows.sort(key=lambda x: (x["risk"] == "High", x["load_percent"], x["overdue"]), reverse=True)
    team_load = safe_int(sum(safe_float(r.get("load_percent"), 0.0) for r in rows) / len(rows), 0) if rows else 0
    return {"team": rows, "summary": {"team_load_percent": team_load, "overloaded_resources": sum(1 for r in rows if r["load_percent"] >= 100), "active_resources": len(rows)}}


def v8_insights_from_df(df: pd.DataFrame, keys: Dict[str, Optional[str]]) -> Dict[str, Any]:
    enriched = enrich_quality(df, keys) if "_quality_score" not in df.columns else df.copy()
    cap = capacity_from_df(enriched, keys)
    total = int(len(enriched)); ready = int((enriched["_readiness"] == "Ready").sum()) if total else 0
    needs = int((enriched["_readiness"] == "Needs Clarification").sum()) if total else 0
    not_ready = int((enriched["_readiness"] == "Not Ready").sum()) if total else 0
    high_risk_df = enriched[(enriched.get("_is_overdue", False)) | (enriched["_quality_score"] < 50)] if total else enriched
    charts = {"readiness": series_counts(enriched, "_readiness"), "confidence": series_counts(enriched, "_confidence")}
    assignee_col = keys.get("assignee"); assignee_risk = {r["assignee"]: r for r in cap.get("team", [])}
    visible = [c for c in [keys.get("reference"), keys.get("demand"), keys.get("status"), keys.get("priority"), assignee_col, keys.get("due")] if c]
    risks = []
    if total:
        tmp = enriched.copy(); tmp["_risk_sort"] = tmp["_is_overdue"].astype(int) * 50 + (100 - tmp["_quality_score"]) + tmp["_age_days"].fillna(0).clip(0, 90) / 3
        for _, r in tmp.sort_values("_risk_sort", ascending=False).head(15).iterrows():
            assignee = _text_value(r, assignee_col) or "Unassigned"; cap_risk = assignee_risk.get(assignee, {}).get("risk", "Unknown")
            risks.append({"row_id": r.get("_row_id"), "row_number": r.get("_row_number"), "quality_score": int(r.get("_quality_score") or 0), "readiness": r.get("_readiness", ""), "confidence": r.get("_confidence", ""), "capacity_risk": cap_risk, "reason": r.get("_quality_reason", ""), "fields": {c: r.get(c, "") for c in visible}})
    return {"summary": {"total_demands": total, "ready_percent": round((ready/total)*100,1) if total else 0, "ready": ready, "needs_clarification": needs, "not_ready": not_ready, "high_risk": int(len(high_risk_df)) if total else 0, "team_load_percent": cap.get("summary",{}).get("team_load_percent",0), "overloaded_resources": cap.get("summary",{}).get("overloaded_resources",0)}, "quality_columns": quality_columns(keys, enriched), "capacity": cap, "charts": charts, "risk_items": risks, "rows": clean_public_df(enriched).to_dict("records")}



def get_governance_sheet(include: str = "objectValue") -> Dict[str, Any]:
    if not GOVERNANCE_SHEET_ID:
        raise HTTPException(
            status_code=500,
            detail="SMARTSHEET_GOVERNANCE_SHEET_ID is not set in .env. Create the Governance Register sheet first and set this value."
        )
    params = {"include": include} if include else {}
    try:
        return smartsheet("GET", f"/sheets/{GOVERNANCE_SHEET_ID}", params=params)
    except HTTPException as exc:
        if include:
            try:
                return smartsheet("GET", f"/sheets/{GOVERNANCE_SHEET_ID}", params={})
            except HTTPException:
                raise exc
        raise


def governance_template_columns() -> List[Dict[str, str]]:
    """
    Enterprise Governance Register template.
    The source ASR sheet is read-only for governance discussions.
    All governance updates are written into this Governance Register sheet.
    Missing columns are auto-created before submit.
    """
    text = "TEXT_NUMBER"
    date = "DATE"
    pick = "PICKLIST"
    return [
        {"title": "Governance Record ID", "type": text},
        {"title": "Source Sheet ID", "type": text},
        {"title": "Source Row Number", "type": text},
        {"title": "Source Row ID", "type": text},
        {"title": "Source Sheet Link", "type": text},
        {"title": "ASR Numbers Auto", "type": text},
        {"title": "ASR Number", "type": text},
        {"title": "Demand", "type": text},
        {"title": "Demand Title", "type": text},
        {"title": "Initiative Status", "type": text},
        {"title": "Analysis", "type": text},
        {"title": "Portfolio / Domain", "type": text},
        {"title": "Requestor", "type": text},
        {"title": "Owner / Assignee", "type": text},
        {"title": "Priority", "type": text},
        {"title": "Created Date", "type": date},
        {"title": "Current Health", "type": text},
        {"title": "Business Outcome", "type": text},
        {"title": "Urgency Reason", "type": text},
        {"title": "Scope Clear", "type": pick},
        {"title": "Scope Clear Detail", "type": text},
        {"title": "Dependencies Known", "type": pick},
        {"title": "Dependencies Known Detail", "type": text},
        {"title": "Data Available", "type": pick},
        {"title": "Data Available Detail", "type": text},
        {"title": "API Ready", "type": pick},
        {"title": "API Ready Detail", "type": text},
        {"title": "NFR Defined", "type": pick},
        {"title": "NFR Defined Detail", "type": text},
        {"title": "Readiness Gaps", "type": text},
        {"title": "Capacity Impact", "type": pick},
        {"title": "Target PI", "type": text},
        {"title": "Last RTE Update", "type": text},
        {"title": "RTE Discussion Date", "type": date},
        {"title": "RTE Discussion Summary", "type": text},
        {"title": "Meeting Attendees", "type": text},
        {"title": "Demand Readiness", "type": pick},
        {"title": "Readiness Score", "type": text},
        {"title": "Readiness Detail Summary", "type": text},
        {"title": "Business Value", "type": text},
        {"title": "Time Criticality", "type": text},
        {"title": "Risk Reduction", "type": text},
        {"title": "Job Size", "type": text},
        {"title": "WSJF Score", "type": text},
        {"title": "RTE Recommendation", "type": text},
        {"title": "Stakeholder Decision", "type": pick},
        {"title": "Governance Discussion Notes", "type": text},
        {"title": "Governance Decision", "type": text},
        {"title": "Action Required", "type": text},
        {"title": "Next Action", "type": text},
        {"title": "Action Owner", "type": text},
        {"title": "Action Due Date", "type": date},
    ]

def governance_picklist_options(title: str) -> Optional[List[str]]:
    return {
        "Demand Readiness": ["Ready", "Not Ready"],
        "Scope Clear": ["Yes", "No"],
        "Dependencies Known": ["Yes", "No"],
        "Data Available": ["Yes", "No"],
        "API Ready": ["Yes", "No"],
        "NFR Defined": ["Yes", "No"],
        "Capacity Impact": ["Low", "Medium", "High"],
        "Stakeholder Decision": [
            "Commit",
            "Refine",
            "Defer",
            "Reject",
            "Escalate",
            "Needs Architecture Review",
            "Needs Business Case",
            "Needs Capacity Trade-off",
        ],
    }.get(title)


def governance_template_status() -> Dict[str, Any]:
    sheet = get_governance_sheet(include="")
    _, by_title = column_maps(sheet)
    expected = governance_template_columns()
    missing = [c for c in expected if c["title"].strip().lower() not in by_title]
    existing = [c["title"] for c in expected if c["title"].strip().lower() in by_title]
    return {
        "governance_sheet_name": sheet.get("name"),
        "governance_sheet_id": GOVERNANCE_SHEET_ID,
        "governance_sheet_link": sheet.get("permalink") or GOVERNANCE_SHEET_LINK,
        "expected_columns": expected,
        "existing_columns": existing,
        "missing_columns": missing,
        "is_ready": len(missing) == 0,
    }



def ensure_governance_register_template() -> Dict[str, Any]:
    """
    Auto-add missing Governance Register columns so submit does not fail.

    Important Smartsheet fix:
    Smartsheet can reject a bulk column-create request when the submitted
    column indexes are not aligned with the first input column index. To avoid
    error 1135, columns are added one-by-one at the current end of the sheet.
    After each add, the sheet is refreshed so the next index is always correct.
    """
    added_columns: List[str] = []

    # Loop because the sheet structure changes after every added column.
    for expected_col in governance_template_columns():
        sheet = get_governance_sheet(include="")
        _, by_title = column_maps(sheet)

        title = expected_col["title"]
        key = title.strip().lower()
        if key in by_title:
            continue

        column_payload = {
            "title": title,
            "type": expected_col.get("type") or "TEXT_NUMBER",
            "index": len(sheet.get("columns", [])),
        }

        options = governance_picklist_options(title)
        if options:
            column_payload["options"] = options

        try:
            smartsheet("POST", f"/sheets/{GOVERNANCE_SHEET_ID}/columns", json=[column_payload])
            added_columns.append(title)
        except HTTPException as exc:
            # If another deploy/user added it between refresh and create, continue safely.
            latest_sheet = get_governance_sheet(include="")
            _, latest_by_title = column_maps(latest_sheet)
            if key in latest_by_title:
                continue
            raise exc

    final_sheet = get_governance_sheet(include="")
    _, final_by_title = column_maps(final_sheet)
    still_missing = [
        c["title"] for c in governance_template_columns()
        if c["title"].strip().lower() not in final_by_title
    ]

    return {
        "status": "ready" if not added_columns else "repaired",
        "added_columns": added_columns,
        "still_missing_columns": still_missing,
        "governance_sheet_id": GOVERNANCE_SHEET_ID,
    }


def _normalise_key(value: Any) -> str:
    """Normalise values for Governance Register matching."""
    return str(value or "").strip().upper()


def find_governance_register_row(
    gov_sheet: Dict[str, Any],
    source_row_id: Any,
    source_row_number: Any = None,
    asr_number: str = "",
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    Find the existing Governance Register row to update.

    Matching priority:
    1. ASR Numbers Auto / ASR Number - this is the business key requested by the user.
    2. Source Row ID - fallback if ASR is blank.
    3. Source Row Number - final fallback for older register rows.
    """
    by_id, _ = column_maps(gov_sheet)
    target_asr = _normalise_key(asr_number)
    target_row_id = _normalise_key(source_row_id)
    target_row_no = _normalise_key(source_row_number)

    fallback = None
    for row in gov_sheet.get("rows", []):
        rec = row_to_dict(row, by_id)
        row_asr_1 = _normalise_key(rec.get("ASR Numbers Auto"))
        row_asr_2 = _normalise_key(rec.get("ASR Number"))
        row_source_id = _normalise_key(rec.get("Source Row ID"))
        row_source_no = _normalise_key(rec.get("Source Row Number"))

        if target_asr and (row_asr_1 == target_asr or row_asr_2 == target_asr):
            return row, rec

        if target_row_id and row_source_id == target_row_id:
            fallback = fallback or (row, rec)

        if not fallback and target_row_no and row_source_no == target_row_no:
            fallback = (row, rec)

    return fallback


def latest_governance_for_source_row(source_row_id: int) -> Optional[Dict[str, Any]]:
    """Return the existing Governance Register row for a source demand row, if present."""
    if not GOVERNANCE_SHEET_ID:
        return None
    try:
        source_sheet = get_sheet(include="objectValue")
        source_by_id, _ = column_maps(source_sheet)
        keys = infer_key_columns([c.get("title", "") for c in source_sheet.get("columns", [])])
        source_row = next((r for r in source_sheet.get("rows", []) if int(r.get("id")) == int(source_row_id)), None)
        source_record = row_to_dict(source_row, source_by_id) if source_row else {}
        ref_col = keys.get("reference")
        asr_number = str(source_record.get(ref_col, "") or "") if ref_col else ""

        gov_sheet = get_governance_sheet(include="objectValue")
        match = find_governance_register_row(
            gov_sheet,
            source_row_id=source_row_id,
            source_row_number=source_row.get("rowNumber") if source_row else None,
            asr_number=asr_number,
        )
        if not match:
            return None
        row, rec = match
        rec["_governance_row_id"] = row.get("id")
        rec["_governance_row_number"] = row.get("rowNumber")
        return rec
    except Exception:
        return None


def create_governance_register_row(source_row_id: int, payload: GovernancePayload) -> Dict[str, Any]:
    """
    Upsert a governance record in the separate Governance Register sheet.
    The source ASR sheet is not modified.

    Important: if a Governance Register row already exists for the ASR Number,
    the row is updated instead of creating duplicates.
    """
    source_sheet = get_sheet(include="objectValue")
    source_by_id, _ = column_maps(source_sheet)
    keys = infer_key_columns([c.get("title", "") for c in source_sheet.get("columns", [])])

    source_row = None
    for row in source_sheet.get("rows", []):
        if int(row.get("id")) == int(source_row_id):
            source_row = row
            break

    if source_row is None:
        raise HTTPException(status_code=404, detail=f"Source row {source_row_id} not found in source ASR sheet")

    source_record = row_to_dict(source_row, source_by_id)

    ensure_result = ensure_governance_register_template()
    gov_sheet = get_governance_sheet(include="")
    _, gov_by_title = column_maps(gov_sheet)

    readiness = governance_readiness(payload)
    wsjf = governance_wsjf(payload)
    recommendation = governance_recommendation(payload, readiness["status"], wsjf)

    def source_value(key: str) -> str:
        col = keys.get(key)
        return str(source_record.get(col, "") or "") if col else ""

    asr_number = source_value("reference")
    demand_title = source_value("demand")
    gov_sheet_for_match = get_governance_sheet(include="objectValue")
    existing_match = find_governance_register_row(
        gov_sheet_for_match,
        source_row_id=source_row.get("id"),
        source_row_number=source_row.get("rowNumber"),
        asr_number=asr_number,
    )
    existing_row = existing_match[0] if existing_match else None
    existing_record = existing_match[1] if existing_match else {}

    governance_record_id = (
        existing_record.get("Governance Record ID")
        or f"{asr_number or source_row_id}-GOV"
    )

    def fmt_date(v: Any) -> Any:
        dt = parse_date(v)
        return dt.strftime("%Y-%m-%d") if dt is not None else ""

    values = {
        "Governance Record ID": governance_record_id,
        "Source Sheet ID": SHEET_ID,
        "Source Row Number": source_row.get("rowNumber"),
        "Source Row ID": source_row.get("id"),
        "Source Sheet Link": SHEET_LINK,
        "ASR Numbers Auto": asr_number,
        "ASR Number": asr_number,
        "Demand": demand_title,
        "Demand Title": demand_title,
        "Initiative Status": source_value("initiative_status") or source_value("status"),
        "Analysis": source_value("analysis"),
        "Portfolio / Domain": source_value("portfolio"),
        "Requestor": source_value("requestor"),
        "Owner / Assignee": source_value("assignee"),
        "Priority": source_value("priority"),
        "Created Date": fmt_date(source_record.get(keys.get("created"), "") if keys.get("created") else source_row.get("createdAt")),
        "Current Health": source_record.get("_health", ""),

        "Business Outcome": payload.business_outcome,
        "Urgency Reason": payload.urgency_reason,
        "Scope Clear": "Yes" if payload.scope_clear else "No",
        "Scope Clear Detail": payload.scope_clear_detail,
        "Dependencies Known": "Yes" if payload.dependencies_known else "No",
        "Dependencies Known Detail": payload.dependencies_known_detail,
        "Data Available": "Yes" if payload.data_available else "No",
        "Data Available Detail": payload.data_available_detail,
        "API Ready": "Yes" if payload.api_ready else "No",
        "API Ready Detail": payload.api_ready_detail,
        "NFR Defined": "Yes" if payload.nfr_defined else "No",
        "NFR Defined Detail": payload.nfr_defined_detail,
        "Readiness Gaps": readiness["gaps"],
        "Capacity Impact": payload.capacity_impact,
        "Target PI": payload.target_pi,
        "Last RTE Update": datetime.now().strftime("%Y-%m-%d %H:%M"),

        "RTE Discussion Date": datetime.now().strftime("%Y-%m-%d"),
        "RTE Discussion Summary": payload.discussion_summary,
        "Meeting Attendees": payload.meeting_attendees,
        "Demand Readiness": readiness["status"],
        "Readiness Score": readiness["score"],
        "Readiness Detail Summary": (
            f"Business Outcome: {payload.business_outcome or 'Not captured'}\n"
            f"Urgency Reason: {payload.urgency_reason or 'Not captured'}\n\n"
            f"{readiness['detail_summary']}\n\n"
            f"Readiness Gaps: {readiness['gaps']}"
        ),
        "Business Value": payload.business_value,
        "Time Criticality": payload.time_criticality,
        "Risk Reduction": payload.risk_reduction,
        "Job Size": payload.job_size,
        "WSJF Score": wsjf,
        "RTE Recommendation": recommendation,
        "Stakeholder Decision": payload.stakeholder_decision,
        "Governance Discussion Notes": payload.discussion_summary,
        "Governance Decision": payload.stakeholder_decision,
        "Action Required": payload.next_action,
        "Next Action": payload.next_action,
        "Action Owner": payload.action_owner,
    }

    missing_template_columns = []
    cells = []
    for title, value in values.items():
        col = gov_by_title.get(title.strip().lower())
        if col:
            cells.append({"columnId": col["id"], "value": value, "strict": False})
        else:
            missing_template_columns.append(title)

    if missing_template_columns:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Governance Register template is missing required columns even after auto-repair. Check Smartsheet permissions to add columns.",
                "missing_columns": missing_template_columns,
                "governance_sheet_id": GOVERNANCE_SHEET_ID,
            },
        )

    if existing_row:
        result = smartsheet(
            "PUT",
            f"/sheets/{GOVERNANCE_SHEET_ID}/rows",
            json=[{"id": existing_row.get("id"), "cells": cells}],
        )
        operation = "updated"
        user_message = f"Governance Register updated for {asr_number or source_row_id}. No duplicate row was created."
        governance_row_id = existing_row.get("id")
    else:
        result = smartsheet(
            "POST",
            f"/sheets/{GOVERNANCE_SHEET_ID}/rows",
            json=[{"toTop": True, "cells": cells}],
        )
        operation = "created"
        user_message = f"Governance Register created for {asr_number or source_row_id}."
        try:
            governance_row_id = (result.get("result") or [{}])[0].get("id")
        except Exception:
            governance_row_id = None

    # Governance updates intentionally write to the Governance Register only.
    # The source demand sheet remains read-only in the governance workflow.
    source_comment_added = False

    return {
        "status": operation,
        "message": user_message,
        "governance_record_id": governance_record_id,
        "governance_row_id": governance_row_id,
        "source_row_id": source_row_id,
        "source_asr_number": asr_number,
        "readiness": readiness,
        "wsjf_score": wsjf,
        "rte_recommendation": recommendation,
        "governance_sheet_id": GOVERNANCE_SHEET_ID,
        "governance_sheet_link": gov_sheet.get("permalink") or GOVERNANCE_SHEET_LINK,
        "smartsheet": result,
        "source_comment_added": source_comment_added,
        "template_check": ensure_result,
    }


def governance_readiness(payload: GovernancePayload) -> Dict[str, Any]:
    checks = {
        "Scope Clear": {
            "ok": payload.scope_clear,
            "detail": payload.scope_clear_detail,
            "column": "Scope Clear Detail",
        },
        "Dependencies Known": {
            "ok": payload.dependencies_known,
            "detail": payload.dependencies_known_detail,
            "column": "Dependencies Known Detail",
        },
        "Data / Access Available": {
            "ok": payload.data_available,
            "detail": payload.data_available_detail,
            "column": "Data Available Detail",
        },
        "API / Integration Ready": {
            "ok": payload.api_ready,
            "detail": payload.api_ready_detail,
            "column": "API Ready Detail",
        },
        "NFRs Defined": {
            "ok": payload.nfr_defined,
            "detail": payload.nfr_defined_detail,
            "column": "NFR Defined Detail",
        },
    }

    gaps = [k for k, v in checks.items() if not v["ok"]]
    missing_details = [k for k, v in checks.items() if v["ok"] and not str(v.get("detail", "")).strip()]
    ready_count = sum(1 for v in checks.values() if v["ok"])

    status = "Ready" if not gaps else "Not Ready"

    return {
        "status": status,
        "score": round((ready_count / len(checks)) * 100, 0),
        "gaps": ", ".join(gaps) if gaps else "None",
        "missing_details": ", ".join(missing_details) if missing_details else "None",
        "checks": checks,
        "detail_summary": "\n".join([
            f"{name}: {'Yes' if item['ok'] else 'No'} - {str(item.get('detail', '')).strip() or 'No detail captured'}"
            for name, item in checks.items()
        ]),
    }


def governance_wsjf(payload: GovernancePayload) -> float:
    return round((int(payload.business_value) + int(payload.time_criticality) + int(payload.risk_reduction)) / max(1, int(payload.job_size or 1)), 2)


def governance_recommendation(payload: GovernancePayload, readiness: str, wsjf: float) -> str:
    if payload.recommendation_override:
        return payload.recommendation_override
    impact = (payload.capacity_impact or "Medium").strip().lower()
    if readiness != "Ready":
        return "Refine"
    if wsjf >= 7 and impact in {"low", "medium"}:
        return "Commit"
    if wsjf >= 7 and impact == "high":
        return "Escalate / Capacity Trade-off Required"
    if 4 <= wsjf < 7:
        return "Defer / Reassess Priority"
    return "Reject / Park"


def update_existing_smartsheet_columns(row_id: int, values: Dict[str, Any]) -> Dict[str, Any]:
    sheet = get_sheet(include="")
    _, by_title = column_maps(sheet)
    cells = []
    missing = []
    for title, value in values.items():
        col = by_title.get(title.strip().lower())
        if col:
            cells.append({"columnId": col["id"], "value": value, "strict": False})
        else:
            missing.append(title)
    if not cells:
        raise HTTPException(status_code=400, detail={"message": "No governance columns were found in Smartsheet. Add the recommended columns first.", "missing_columns": missing})
    result = smartsheet("PUT", f"/sheets/{SHEET_ID}/rows", json=[{"id": row_id, "cells": cells}])
    return {"result": result, "updated_columns": [k for k in values.keys() if k not in missing], "missing_columns": missing}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "sheet_id": SHEET_ID,
        "sheet_link": SHEET_LINK,
        "governance_sheet_id": GOVERNANCE_SHEET_ID or "Not configured",
        "governance_sheet_link": GOVERNANCE_SHEET_LINK or "#",
    })


@app.get("/api/sheet")
def api_sheet():
    sheet, df, keys = build_dataframe()
    public = clean_public_df(enrich_quality(df, keys))
    return {"sheet_name": sheet.get("name"), "sheet_id": SHEET_ID, "permalink": sheet.get("permalink") or SHEET_LINK, "columns": sheet.get("columns", []), "column_titles": [c["title"] for c in sheet.get("columns", [])], "key_columns": keys, "rows": public.to_dict("records")}


@app.get("/api/dashboard")
def api_dashboard():
    sheet, df, keys = build_dataframe()
    enriched = enrich_quality(df, keys)
    dash = dashboard_from_df(sheet, enriched, keys)
    v8 = v8_insights_from_df(enriched, keys)
    dash["v8"] = v8
    dash["metrics"].update({"ready_percent": v8["summary"]["ready_percent"], "not_ready": v8["summary"]["not_ready"], "high_risk": v8["summary"]["high_risk"], "team_load_percent": v8["summary"]["team_load_percent"], "overloaded_resources": v8["summary"]["overloaded_resources"]})
    dash["charts"].update(v8.get("charts", {}))
    dash["dashboard_scope"] = "full"
    return dash


def resolve_dashboard_filter_payload(payload: FilterPayload, keys: Dict[str, Optional[str]], df: pd.DataFrame) -> FilterPayload:
    """Resolve lightweight UI aliases such as __key__:analysis to real columns.

    This lets the Executive Dashboard open without first loading the full sheet
    in the browser. The backend knows the actual Smartsheet columns after it
    builds the combined demand + governance dataframe.
    """
    resolved_rules = []
    for rule in payload.rules:
        col = rule.column
        if isinstance(col, str) and col.startswith("__key__:"):
            key = col.split(":", 1)[1]
            col = keys.get(key) or col
        # Keep governance/system columns unchanged. Drop only aliases that still
        # do not resolve to an actual dataframe column.
        if col in df.columns:
            resolved_rules.append(FilterRule(column=col, operator=rule.operator, value=rule.value))
    return FilterPayload(rules=resolved_rules, logic=payload.logic, limit=payload.limit)


@app.post("/api/dashboard/filter")
def api_dashboard_filter(payload: FilterPayload):
    """Build the Executive Governance Dashboard from selected criteria.

    This endpoint now combines the source ASR demand sheet and the Governance
    Register. Demand columns remain unchanged. Governance Register columns are
    exposed as `Governance - <Column Name>`, so criteria can target either what
    was logged or what was committed after governance discussions.
    """
    sheet, df, keys = build_governance_dashboard_dataframe()
    enriched_all = enrich_quality(df, keys)
    payload = resolve_dashboard_filter_payload(payload, keys, enriched_all)
    filtered = apply_filter_rules(enriched_all, payload) if payload.rules else enriched_all
    enriched = enrich_quality(filtered, keys)
    dashboard = dashboard_from_df(sheet, enriched, keys)
    v8 = v8_insights_from_df(enriched, keys)
    gov = governance_dashboard_insights(enriched, keys)
    dashboard["v8"] = v8
    dashboard["governance"] = gov
    dashboard["metrics"].update({"ready_percent": v8["summary"]["ready_percent"], "not_ready": v8["summary"]["not_ready"], "high_risk": v8["summary"]["high_risk"], "team_load_percent": v8["summary"]["team_load_percent"], "overloaded_resources": v8["summary"]["overloaded_resources"]})
    dashboard["metrics"].update(gov.get("metrics", {}))
    dashboard["charts"].update(v8.get("charts", {}))
    dashboard["charts"].update(gov.get("charts", {}))
    dashboard["dashboard_scope"] = "criteria" if payload.rules else "full"
    public = clean_public_df(enriched).head(max(1, min(payload.limit, 1000))).to_dict("records")
    dashboard_columns = [c for c in enriched.columns if not c.endswith("_parsed")]
    return {"summary": dashboard["metrics"], "dashboard": dashboard, "rows": public, "key_columns": keys, "columns": dashboard_columns, "applied_rules": [r.model_dump() for r in payload.rules], "logic": payload.logic}


@app.get("/api/governance-dashboard/columns")
def api_governance_dashboard_columns():
    sheet, df, keys = build_governance_dashboard_dataframe()
    enriched = enrich_quality(df, keys)
    demand_cols = [c for c in sheet.get("columns", [])]
    return {
        "demand_columns": [c.get("title") for c in demand_cols],
        "governance_columns": sorted([c for c in enriched.columns if c.startswith("Governance - ")]),
        "system_columns": ["_health", "_age_days", "_age_bucket", "_is_overdue", "_is_closed", "_readiness", "_confidence", "_has_governance_commitment", "_has_open_action", "_governance_action_overdue"],
        "key_columns": keys,
    }


@app.post("/api/filter")
def api_filter(payload: FilterPayload):
    sheet, df, keys = build_dataframe()
    filtered = apply_filter_rules(df, payload)
    enriched = enrich_quality(filtered, keys)
    dashboard = dashboard_from_df(sheet, enriched, keys)
    v8 = v8_insights_from_df(enriched, keys)
    dashboard["v8"] = v8
    dashboard["metrics"].update({"ready_percent": v8["summary"]["ready_percent"], "not_ready": v8["summary"]["not_ready"], "high_risk": v8["summary"]["high_risk"], "team_load_percent": v8["summary"]["team_load_percent"], "overloaded_resources": v8["summary"]["overloaded_resources"]})
    dashboard["charts"].update(v8.get("charts", {}))
    public = clean_public_df(enriched).head(max(1, min(payload.limit, 1000))).to_dict("records")
    return {"summary": dashboard["metrics"], "dashboard": dashboard, "rows": public, "key_columns": keys, "columns": [c["title"] for c in sheet.get("columns", [])] + ["_health", "_age_days", "_age_bucket", "_is_overdue", "_quality_score", "_readiness", "_confidence"], "applied_rules": [r.model_dump() for r in payload.rules], "logic": payload.logic}


@app.get("/api/search")
def api_search(q: str = Query(""), status: str = Query(""), assignee: str = Query(""), priority: str = Query(""), health: str = Query(""), limit: int = Query(100, ge=1, le=500)):
    _, df, keys = build_dataframe()
    filtered = df.copy()
    terms = [t.lower() for t in re.findall(r'"([^"]+)"|(\S+)', q) for t in (t if isinstance(t, tuple) else (t,)) if t]
    if terms:
        searchable = filtered.fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        for term in terms:
            filtered = filtered[searchable.loc[filtered.index].str.contains(re.escape(term), na=False)]
    for selected, key in [(status, keys.get("status")), (assignee, keys.get("assignee")), (priority, keys.get("priority"))]:
        if selected and key in filtered.columns:
            filtered = filtered[filtered[key].fillna("").astype(str).str.lower() == selected.lower()]
    if health:
        filtered = filtered[filtered["_health"].fillna("").astype(str).str.lower() == health.lower()]
    summary = {"count": int(len(filtered)), "open": int((~filtered["_is_closed"]).sum()) if len(filtered) else 0, "overdue": int(filtered["_is_overdue"].sum()) if len(filtered) else 0, "avg_age": round(float(filtered["_age_days"].dropna().mean()), 1) if len(filtered) and not filtered["_age_days"].dropna().empty else 0}
    result = clean_public_df(filtered).head(limit).to_dict("records")
    return {"summary": summary, "rows": result, "key_columns": keys}


@app.get("/api/interrogate/suggestions")
def api_interrogate_suggestions():
    _, df, keys = build_dataframe()
    return {"suggestions": [
        "Show all overdue open demands",
        "Show demands older than 30 days",
        "Which assignees have the highest open workload?",
        "Show high priority demands not closed",
        "Find blank assignee or owner records",
        "Show demand health by status",
    ], "filters": {"status": [x["name"] for x in series_counts(df, keys.get("status"), 50)], "priority": [x["name"] for x in series_counts(df, keys.get("priority"), 50)], "assignee": [x["name"] for x in series_counts(df, keys.get("assignee"), 100)], "health": ["Healthy", "Aging", "Overdue", "Closed"]}}


@app.post("/api/rows")
def add_row(payload: DemandPayload):
    sheet = get_sheet(include="")
    _, by_title = column_maps(sheet)
    cells = []
    for title, value in payload.values.items():
        col = by_title.get(title.strip().lower())
        if col and value not in [None, ""]:
            cells.append({"columnId": col["id"], "value": value, "strict": False})
    if not cells:
        raise HTTPException(status_code=400, detail="No valid Smartsheet columns found in submitted values")
    res = smartsheet("POST", f"/sheets/{SHEET_ID}/rows", json=[{"toTop": payload.to_top, "cells": cells}])
    invalidate_sheet_cache()
    return res


@app.put("/api/rows/{row_id}")
def update_row(row_id: int, payload: UpdatePayload):
    sheet = get_sheet(include="")
    _, by_title = column_maps(sheet)
    cells = []
    for title, value in payload.values.items():
        col = by_title.get(title.strip().lower())
        if col:
            cells.append({"columnId": col["id"], "value": value, "strict": False})
    if not cells:
        raise HTTPException(status_code=400, detail="No valid Smartsheet columns found in submitted values")
    res = smartsheet("PUT", f"/sheets/{SHEET_ID}/rows", json=[{"id": row_id, "cells": cells}])
    invalidate_sheet_cache()
    return res


@app.get("/api/rows/{row_id}")
def get_row_detail(row_id: int):
    sheet = get_sheet(include="objectValue")
    by_id, _ = column_maps(sheet)
    target = None
    for row in sheet.get("rows", []):
        if int(row.get("id")) == int(row_id):
            target = row
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"Row {row_id} not found in sheet")

    record = row_to_dict(target, by_id)
    cell_by_column_id = {cell.get("columnId"): cell for cell in target.get("cells", [])}
    fields = []
    for col in sheet.get("columns", []):
        cid = col.get("id")
        cell = cell_by_column_id.get(cid, {})
        fields.append({
            "column_id": cid,
            "title": col.get("title"),
            "type": col.get("type"),
            "primary": bool(col.get("primary")),
            "locked": bool(col.get("locked") or col.get("lockedForUser")),
            "options": col.get("options") or [],
            "value": cell.get("value", ""),
            "displayValue": cell.get("displayValue", cell.get("value", "")),
            "objectValue": cell.get("objectValue"),
        })

    keys = infer_key_columns([c.get("title", "") for c in sheet.get("columns", [])])
    tmp_df = pd.DataFrame([record])
    tmp_df["_is_closed"] = tmp_df[keys.get("status")].apply(is_closed) if keys.get("status") in tmp_df.columns else False
    tmp_df["_age_days"] = tmp_df[keys.get("created")].apply(lambda v: safe_age_days(parse_date(v))) if keys.get("created") in tmp_df.columns else tmp_df.get("_created_at", pd.Series(dtype=str)).apply(lambda v: safe_age_days(parse_date(v)))
    tmp_df["_is_overdue"] = False
    quality = score_one_demand(tmp_df.iloc[0], quality_columns(keys, tmp_df))
    return {"row_id": target.get("id"), "row_number": target.get("rowNumber"), "created_at": target.get("createdAt"), "modified_at": target.get("modifiedAt"), "record": record, "fields": fields, "key_columns": keys, "quality": quality}


@app.get("/api/rows/{row_id}/comments")
def get_row_comments(row_id: int):
    data = smartsheet("GET", f"/sheets/{SHEET_ID}/rows/{row_id}/discussions", params={"include": "comments"})
    discussions = data.get("data", data.get("discussions", [])) if isinstance(data, dict) else []
    comments = []
    for discussion in discussions or []:
        for comment in discussion.get("comments", []) or []:
            created_by = comment.get("createdBy") or comment.get("modifiedBy") or {}
            comments.append({"discussion_id": discussion.get("id"), "comment_id": comment.get("id"), "text": comment.get("text", ""), "created_at": comment.get("createdAt") or comment.get("modifiedAt") or "", "created_by": created_by.get("name") or created_by.get("email") or "Smartsheet User", "email": created_by.get("email", "")})
    comments.sort(key=lambda x: x.get("created_at") or "")
    return {"row_id": row_id, "count": len(comments), "comments": comments, "raw_discussion_count": len(discussions or [])}


@app.post("/api/rows/{row_id}/comments")
def add_row_comment(row_id: int, payload: CommentPayload):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Comment text is required")
    return smartsheet("POST", f"/sheets/{SHEET_ID}/rows/{row_id}/discussions", json={"comment": {"text": text}})


@app.delete("/api/rows/{row_id}")
def delete_row(row_id: int):
    res = smartsheet("DELETE", f"/sheets/{SHEET_ID}/rows", params={"ids": row_id})
    invalidate_sheet_cache()
    return res


@app.get("/api/quality_scores")
def api_quality_scores():
    _, df, keys = build_dataframe()
    enriched = enrich_quality(df, keys)
    v8 = v8_insights_from_df(enriched, keys)
    return {"summary": v8["summary"], "quality_columns": v8["quality_columns"], "rows": v8["rows"], "charts": v8["charts"]}


@app.get("/api/capacity")
def api_capacity():
    _, df, keys = build_dataframe()
    return capacity_from_df(enrich_quality(df, keys), keys)


@app.get("/api/risk")
def api_risk():
    _, df, keys = build_dataframe()
    return v8_insights_from_df(enrich_quality(df, keys), keys)


@app.get("/api/insights")
def api_insights():
    _, df, keys = build_dataframe()
    v8 = v8_insights_from_df(enrich_quality(df, keys), keys); summary = v8["summary"]
    insights = []
    if summary["not_ready"]: insights.append(f"{summary['not_ready']} demands are not ready. Prioritise ownership, due dates, impacted systems and compliance metadata.")
    if summary["overloaded_resources"]: insights.append(f"{summary['overloaded_resources']} resources appear overloaded based on weighted demand load.")
    if summary["high_risk"]: insights.append(f"{summary['high_risk']} demands are high risk because they are overdue and/or have poor readiness quality.")
    if not insights: insights.append("No critical demand management issues detected from the current dataset.")
    return {"summary": summary, "insights": insights, "top_risks": v8["risk_items"][:10]}


@app.post("/api/governance/score")
def api_governance_score(payload: GovernancePayload):
    readiness = governance_readiness(payload)
    wsjf = governance_wsjf(payload)
    recommendation = governance_recommendation(payload, readiness["status"], wsjf)
    return {"readiness": readiness, "wsjf_score": wsjf, "rte_recommendation": recommendation}


@app.post("/api/governance/submit/{row_id}")
def api_governance_submit(row_id: int, payload: GovernancePayload):
    return create_governance_register_row(row_id, payload)


@app.get("/api/governance/template-status")
def api_governance_template_status():
    return governance_template_status()



@app.post("/api/governance/template-repair")
def api_governance_template_repair():
    return ensure_governance_register_template()


@app.get("/api/governance/demand/{row_id}/details")
def api_governance_demand_details(row_id: int):
    detail = get_row_detail(row_id)
    comments = get_row_comments(row_id)
    latest_gov = latest_governance_for_source_row(row_id)
    return {
        "row": detail,
        "comments": comments.get("comments", []),
        "comment_count": comments.get("count", 0),
        "latest_governance": latest_gov,
        "write_back_rule": "Governance updates are written to the Governance Register sheet only.",
        "governance_sheet_id": GOVERNANCE_SHEET_ID,
    }

@app.get("/api/governance/recommended-columns")
def api_governance_recommended_columns():
    return {"columns": governance_template_columns()}


@app.get("/api/export/excel")
def export_excel():
    sheet, df, keys = build_dataframe()
    enriched = enrich_quality(df, keys)
    output = io.BytesIO()
    clean = clean_public_df(enriched)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        clean.to_excel(writer, sheet_name="Demand Register", index=False)
        dash = api_dashboard()
        pd.DataFrame([dash["metrics"]]).to_excel(writer, sheet_name="Executive Summary", index=False)
        for name, data in dash["charts"].items():
            pd.DataFrame(data).to_excel(writer, sheet_name=name[:31], index=False)
        pd.DataFrame(api_governance_recommended_columns()["columns"], columns=["Recommended Governance Columns"]).to_excel(writer, sheet_name="Governance Columns", index=False)
    output.seek(0)
    filename = f"asoc_demand_governance_export_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={filename}"})
