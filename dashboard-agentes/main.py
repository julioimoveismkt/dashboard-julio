"""Dashboard de Gestão de Agentes — Julio Imóveis (Fase 1 + Fase 2 + Fase 3 + Chat V2 Fase 3)"""

import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import asyncio
import threading

from fastapi import FastAPI, Request, Response, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

app = FastAPI(title="Dashboard Agentes - Julio Imóveis", version="3.0.0")

BASE_DIR = Path(__file__).parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

# --- Auth Config (T3.1) ---
USERS = {
    "admin": {"password": "julio2026", "role": "admin", "name": "Rafael"},
    "viewer": {"password": "julio2026", "role": "viewer", "name": "Sócios"},
}
SESSIONS: dict[str, dict] = {}  # token -> {user, role, name, created_at}
SESSION_TTL = 86400  # 24h

PUBLIC_PATHS = {"/login", "/api/v1/login", "/static"}


def create_session(username: str) -> str:
    """Create a new session and return the token."""
    token = uuid.uuid4().hex
    user_info = USERS[username]
    SESSIONS[token] = {
        "user": username,
        "role": user_info["role"],
        "name": user_info["name"],
        "created_at": time.time(),
    }
    return token


def get_session(token: str) -> dict | None:
    """Validate and return session, or None if expired/invalid."""
    if not token or token not in SESSIONS:
        return None
    sess = SESSIONS[token]
    if time.time() - sess["created_at"] > SESSION_TTL:
        del SESSIONS[token]
        return None
    return sess


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Allow public paths
        if path == "/login" or path == "/api/v1/login" or path.startswith("/static"):
            return await call_next(request)

        token = request.cookies.get("session_token")
        sess = get_session(token)

        if not sess:
            # API calls get 401, pages get redirect
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=302)

        # Attach session to request state
        request.state.session = sess
        return await call_next(request)


app.add_middleware(AuthMiddleware)

# --- Config ---
with open(BASE_DIR / "agents_config.json") as f:
    AGENTS_CONFIG = json.load(f)["agents"]

PROFILE_PATHS = {
    "default": HERMES_HOME,
    "ji-projetos": HERMES_HOME / "profiles" / "ji-projetos",
    "ji-financeiro": HERMES_HOME / "profiles" / "ji-financeiro",
}


def get_db(profile: str, db_name: str = "state.db"):
    """Open a read-only SQLite connection for a profile."""
    path = PROFILE_PATHS.get(profile, HERMES_HOME) / db_name
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_cron_db(profile: str):
    """Open the cron executions.db read-only."""
    path = PROFILE_PATHS.get(profile, HERMES_HOME) / "cron" / "executions.db"
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_cron_jobs(profile: str):
    """Read cron jobs.json for a profile."""
    path = PROFILE_PATHS.get(profile, HERMES_HOME) / "cron" / "jobs.json"
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("jobs", [])


def get_skills(profile: str):
    """List skills from filesystem."""
    path = PROFILE_PATHS.get(profile, HERMES_HOME) / "skills"
    if not path.exists():
        return []
    skills = []
    for item in sorted(path.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            sub_skills = [s.name for s in item.iterdir() if s.is_dir() and not s.name.startswith(".")]
            if sub_skills:
                for s in sorted(sub_skills):
                    skills.append({"name": s, "category": item.name})
            else:
                skills.append({"name": item.name, "category": None})
    return skills


def get_agent_status(profile: str) -> dict:
    """Determine agent status from last session activity."""
    conn = get_db(profile)
    if not conn:
        return {"status": "offline", "last_activity": None}
    try:
        row = conn.execute(
            "SELECT started_at, last_activity_at, ended_at, title "
            "FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"status": "offline", "last_activity": None}

        last_ts = row["last_activity_at"] or row["ended_at"] or row["started_at"]
        if last_ts and (time.time() - last_ts) < 86400:
            status = "online"
        else:
            status = "offline"
        return {"status": status, "last_activity": last_ts, "last_title": row["title"]}
    finally:
        conn.close()


def _get_agent_config(profile: str):
    """Find agent config by profile name."""
    for cfg in AGENTS_CONFIG:
        if cfg["profile"] == profile:
            return cfg
    return None


# --- Auth Endpoints (T3.1) ---

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return FileResponse(str(BASE_DIR / "static" / "login.html"))


@app.post("/api/v1/login")
async def do_login(request: Request):
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")

    user = USERS.get(username)
    if not user or user["password"] != password:
        return JSONResponse({"error": "Credenciais inválidas"}, status_code=401)

    token = create_session(username)
    response = JSONResponse({"ok": True, "role": user["role"], "name": user["name"]})
    response.set_cookie(
        "session_token", token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/api/v1/logout")
def do_logout(request: Request):
    token = request.cookies.get("session_token")
    if token and token in SESSIONS:
        del SESSIONS[token]
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session_token")
    return response


@app.get("/api/v1/me")
def current_user(request: Request):
    """Return current user info including role for frontend role-based rendering."""
    sess = request.state.session
    return {"user": sess["user"], "role": sess["role"], "name": sess["name"]}


# --- API Endpoints (Fase 1) ---

@app.get("/api/v1/agents")
def list_agents():
    """List all agents with status and counts."""
    agents = []
    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        status_info = get_agent_status(profile)
        skills = get_skills(profile)
        crons = get_cron_jobs(profile)
        active_crons = [c for c in crons if c.get("enabled", True)]

        agents.append({
            "name": cfg["name"],
            "profile": profile,
            "role": cfg["role"],
            "model": cfg["model"],
            "operator": cfg["operator"],
            "description": cfg["description"],
            "status": status_info["status"],
            "last_activity": status_info.get("last_activity"),
            "last_title": status_info.get("last_title"),
            "skills_count": len(skills),
            "crons_count": len(active_crons),
            "systems": cfg["systems"],
        })
    return agents


@app.get("/api/v1/agents/{profile}/skills")
def agent_skills(profile: str):
    """List skills for a specific agent."""
    return get_skills(profile)


@app.get("/api/v1/metrics/summary")
def metrics_summary():
    """Summary metrics for the header cards."""
    total_skills = 0
    total_crons = 0
    sessions_today = 0
    messages_today = 0
    agents_online = 0

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        total_skills += len(get_skills(profile))
        crons = get_cron_jobs(profile)
        total_crons += len([c for c in crons if c.get("enabled", True)])
        status_info = get_agent_status(profile)
        if status_info["status"] == "online":
            agents_online += 1
        conn = get_db(profile)
        if conn:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM sessions WHERE started_at >= ?",
                    (today_start,),
                ).fetchone()
                sessions_today += row["cnt"]

                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages WHERE timestamp >= ?",
                    (today_start,),
                ).fetchone()
                if row:
                    messages_today += row["cnt"]
            except Exception:
                pass
            finally:
                conn.close()

    return {
        "agents_active": agents_online,
        "agents_total": len(AGENTS_CONFIG),
        "total_skills": total_skills,
        "active_crons": total_crons,
        "sessions_today": sessions_today,
        "messages_today": messages_today,
    }


@app.get("/api/v1/crons")
def list_crons():
    """List all crons from all profiles."""
    all_crons = []
    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        jobs = get_cron_jobs(profile)
        for job in jobs:
            all_crons.append({
                "id": job["id"],
                "name": job["name"],
                "profile": profile,
                "agent_name": cfg["name"],
                "schedule": job.get("schedule_display", ""),
                "enabled": job.get("enabled", True),
                "state": job.get("state", "unknown"),
                "last_status": job.get("last_status"),
                "last_run_at": job.get("last_run_at"),
                "next_run_at": job.get("next_run_at"),
                "failure_streak": job.get("failure_streak", 0),
            })
    return all_crons


@app.get("/api/v1/crons/incidents")
def cron_incidents():
    """List cron incidents from all profiles."""
    incidents = []
    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        conn = get_cron_db(profile)
        if not conn:
            continue
        try:
            rows = conn.execute(
                "SELECT * FROM cron_incidents ORDER BY first_seen_at DESC LIMIT 20"
            ).fetchall()
            for row in rows:
                incidents.append({
                    "id": row["id"],
                    "job_id": row["job_id"],
                    "profile": profile,
                    "agent_name": cfg["name"],
                    "state": row["state"],
                    "failure_type": row["failure_type"],
                    "error": row["error"],
                    "first_seen_at": row["first_seen_at"],
                })
        except Exception:
            pass
        finally:
            conn.close()
    return incidents


@app.get("/api/v1/activity")
def recent_activity():
    """Recent activity across all agents."""
    activities = []
    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        conn = get_db(profile)
        if not conn:
            continue
        try:
            rows = conn.execute(
                "SELECT id, title, source, started_at, ended_at, message_count, "
                "tool_call_count, estimated_cost_usd "
                "FROM sessions ORDER BY started_at DESC LIMIT 15"
            ).fetchall()
            for row in rows:
                activities.append({
                    "type": "session",
                    "agent_name": cfg["name"],
                    "profile": profile,
                    "title": row["title"] or "Sessão sem título",
                    "source": row["source"],
                    "timestamp": row["started_at"],
                    "ended_at": row["ended_at"],
                    "message_count": row["message_count"],
                    "tool_call_count": row["tool_call_count"],
                    "cost_usd": row["estimated_cost_usd"],
                })
        except Exception:
            pass
        finally:
            conn.close()

    activities.sort(key=lambda x: x.get("timestamp", 0) or 0, reverse=True)
    return activities[:30]


@app.get("/api/v1/systems")
def connected_systems():
    """Systems connected per agent."""
    systems = []
    for cfg in AGENTS_CONFIG:
        for sys in cfg["systems"]:
            systems.append({
                "name": sys["name"],
                "icon": sys["icon"],
                "status": sys["status"],
                "agent_name": cfg["name"],
                "profile": cfg["profile"],
            })
    return systems


# --- API Endpoints (Fase 2) ---

@app.get("/api/v1/agents/{profile}/details")
def agent_details(profile: str):
    """Full agent details: recent sessions, last activity, brain path, model, skills, crons, systems."""
    cfg = _get_agent_config(profile)
    if not cfg:
        return {"error": "Agent not found"}

    # Basic info
    result = {
        "name": cfg["name"],
        "profile": profile,
        "role": cfg["role"],
        "model": cfg["model"],
        "operator": cfg["operator"],
        "description": cfg["description"],
        "workspace": cfg.get("workspace", ""),
        "systems": cfg["systems"],
        "brain_path": str(PROFILE_PATHS.get(profile, HERMES_HOME)),
    }

    # Skills
    result["skills"] = get_skills(profile)

    # Crons
    crons = get_cron_jobs(profile)
    result["crons"] = []
    for job in crons:
        result["crons"].append({
            "id": job["id"],
            "name": job["name"],
            "schedule": job.get("schedule_display", ""),
            "enabled": job.get("enabled", True),
            "last_status": job.get("last_status"),
            "last_run_at": job.get("last_run_at"),
            "next_run_at": job.get("next_run_at"),
            "failure_streak": job.get("failure_streak", 0),
        })

    # Recent sessions
    result["recent_sessions"] = []
    conn = get_db(profile)
    if conn:
        try:
            rows = conn.execute(
                "SELECT id, title, source, started_at, ended_at, message_count, "
                "tool_call_count, input_tokens, output_tokens, estimated_cost_usd "
                "FROM sessions ORDER BY started_at DESC LIMIT 10"
            ).fetchall()
            for row in rows:
                result["recent_sessions"].append({
                    "id": row["id"],
                    "title": row["title"] or "Sem título",
                    "source": row["source"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "message_count": row["message_count"],
                    "tool_call_count": row["tool_call_count"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cost_usd": row["estimated_cost_usd"],
                })
        except Exception:
            pass
        finally:
            conn.close()

    # Status
    status_info = get_agent_status(profile)
    result["status"] = status_info["status"]
    result["last_activity"] = status_info.get("last_activity")

    # Cron incidents for this profile
    result["incidents"] = []
    cron_conn = get_cron_db(profile)
    if cron_conn:
        try:
            rows = cron_conn.execute(
                "SELECT * FROM cron_incidents ORDER BY first_seen_at DESC LIMIT 10"
            ).fetchall()
            for row in rows:
                result["incidents"].append({
                    "id": row["id"],
                    "job_id": row["job_id"],
                    "state": row["state"],
                    "failure_type": row["failure_type"],
                    "error": row["error"],
                    "first_seen_at": row["first_seen_at"],
                })
        except Exception:
            pass
        finally:
            cron_conn.close()

    return result


@app.get("/api/v1/metrics/tokens")
def metrics_tokens():
    """Token usage aggregated by day and agent for the last 7 days."""
    now = datetime.now(timezone.utc)
    days = []
    for i in range(6, -1, -1):
        d = now - timedelta(days=i)
        days.append(d.strftime("%Y-%m-%d"))

    result = {"days": days, "agents": {}}

    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        agent_name = cfg["name"]
        daily = {d: {"input": 0, "output": 0, "total": 0, "cost": 0.0, "sessions": 0} for d in days}

        conn = get_db(profile)
        if not conn:
            result["agents"][profile] = {"name": agent_name, "daily": daily}
            continue

        try:
            seven_days_ago = (now - timedelta(days=7)).timestamp()
            rows = conn.execute(
                "SELECT started_at, input_tokens, output_tokens, "
                "cache_read_tokens, reasoning_tokens, estimated_cost_usd "
                "FROM sessions WHERE started_at >= ? ORDER BY started_at",
                (seven_days_ago,),
            ).fetchall()

            for row in rows:
                ts = row["started_at"]
                if not ts:
                    continue
                day_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                if day_key in daily:
                    inp = row["input_tokens"] or 0
                    out = row["output_tokens"] or 0
                    daily[day_key]["input"] += inp
                    daily[day_key]["output"] += out
                    daily[day_key]["total"] += inp + out
                    daily[day_key]["cost"] += row["estimated_cost_usd"] or 0.0
                    daily[day_key]["sessions"] += 1
        except Exception:
            pass
        finally:
            conn.close()

        result["agents"][profile] = {"name": agent_name, "daily": daily}

    # Grand totals per day
    result["totals"] = {}
    for d in days:
        t = {"input": 0, "output": 0, "total": 0, "cost": 0.0, "sessions": 0}
        for profile_data in result["agents"].values():
            dd = profile_data["daily"].get(d, {})
            t["input"] += dd.get("input", 0)
            t["output"] += dd.get("output", 0)
            t["total"] += dd.get("total", 0)
            t["cost"] += dd.get("cost", 0.0)
            t["sessions"] += dd.get("sessions", 0)
        result["totals"][d] = t

    return result


@app.get("/api/v1/activity/timeline")
def activity_timeline():
    """Last 50 actions across all agents: sessions, cron executions, errors."""
    events = []

    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        agent_name = cfg["name"]

        # Sessions
        conn = get_db(profile)
        if conn:
            try:
                rows = conn.execute(
                    "SELECT id, title, source, started_at, ended_at, message_count, "
                    "tool_call_count, estimated_cost_usd, input_tokens, output_tokens "
                    "FROM sessions ORDER BY started_at DESC LIMIT 25"
                ).fetchall()
                for row in rows:
                    events.append({
                        "type": "session",
                        "subtype": row["source"] or "unknown",
                        "agent_name": agent_name,
                        "profile": profile,
                        "title": row["title"] or "Sessão sem título",
                        "timestamp": row["started_at"],
                        "ended_at": row["ended_at"],
                        "message_count": row["message_count"],
                        "tool_call_count": row["tool_call_count"],
                        "cost_usd": row["estimated_cost_usd"],
                        "tokens": (row["input_tokens"] or 0) + (row["output_tokens"] or 0),
                    })
            except Exception:
                pass
            finally:
                conn.close()

        # Cron executions
        cron_conn = get_cron_db(profile)
        if cron_conn:
            try:
                rows = cron_conn.execute(
                    "SELECT id, job_id, status, started_at, finished_at, error, delivery_outcome "
                    "FROM executions ORDER BY started_at DESC LIMIT 20"
                ).fetchall()
                for row in rows:
                    ts = row["started_at"]
                    ts_val = None
                    if ts:
                        try:
                            ts_val = datetime.fromisoformat(ts).timestamp()
                        except Exception:
                            ts_val = None

                    events.append({
                        "type": "cron_exec",
                        "subtype": "cron",
                        "agent_name": agent_name,
                        "profile": profile,
                        "title": f"Cron {row['job_id'][:8]}… → {row['status']}",
                        "timestamp": ts_val,
                        "status": row["status"],
                        "error": row["error"],
                        "delivery": row["delivery_outcome"],
                    })
            except Exception:
                pass
            finally:
                cron_conn.close()

        # Cron incidents
        cron_conn2 = get_cron_db(profile)
        if cron_conn2:
            try:
                rows = cron_conn2.execute(
                    "SELECT id, job_id, state, failure_type, error, first_seen_at "
                    "FROM cron_incidents ORDER BY first_seen_at DESC LIMIT 10"
                ).fetchall()
                for row in rows:
                    ts = row["first_seen_at"]
                    ts_val = None
                    if ts:
                        try:
                            ts_val = datetime.fromisoformat(ts).timestamp()
                        except Exception:
                            ts_val = None
                    events.append({
                        "type": "incident",
                        "subtype": "error",
                        "agent_name": agent_name,
                        "profile": profile,
                        "title": f"Incidente: {row['failure_type']} ({row['state']})",
                        "timestamp": ts_val,
                        "error": row["error"],
                    })
            except Exception:
                pass
            finally:
                cron_conn2.close()

    events.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)
    return events[:50]


@app.get("/api/v1/delegations")
def recent_delegations():
    """Recent async delegations across all profiles."""
    delegations = []
    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        conn = get_db(profile)
        if not conn:
            continue
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='async_delegations'"
            ).fetchall()]
            if "async_delegations" not in tables:
                continue

            rows = conn.execute(
                "SELECT delegation_id, state, dispatched_at, completed_at, "
                "task_json, delivery_state "
                "FROM async_delegations ORDER BY dispatched_at DESC LIMIT 10"
            ).fetchall()
            for row in rows:
                goal = ""
                try:
                    task = json.loads(row["task_json"]) if row["task_json"] else {}
                    goal = task.get("goal", "")
                except Exception:
                    pass

                delegations.append({
                    "delegation_id": row["delegation_id"],
                    "agent_name": cfg["name"],
                    "profile": profile,
                    "state": row["state"],
                    "dispatched_at": row["dispatched_at"],
                    "completed_at": row["completed_at"],
                    "delivery_state": row["delivery_state"],
                    "goal": goal[:200] if goal else "",
                    "duration": round(row["completed_at"] - row["dispatched_at"], 1) if row["completed_at"] and row["dispatched_at"] else None,
                })
        except Exception:
            pass
        finally:
            conn.close()

    delegations.sort(key=lambda x: x.get("dispatched_at") or 0, reverse=True)
    return delegations[:20]


# --- Token Usage / Live Monitoring Endpoints ---

def _parse_hermes_usage(profile: str = "default") -> dict:
    """Run 'hermes usage' and parse the output."""
    cmd = ["hermes", "usage"]
    if profile != "default":
        cmd = ["hermes", "--profile", profile, "usage"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = result.stdout
    except Exception as e:
        return {"error": str(e)}

    data = {}
    # Parse session line
    m = re.search(r'Current session:\s*(\d+)%\s*remaining\s*\((\d+)%\s*used\)\s*•\s*resets in\s*([\ddhm ]+)\s*\(([^)]+)\)', output)
    if m:
        data["session_remaining"] = int(m.group(1))
        data["session_used"] = int(m.group(2))
        data["session_reset_in"] = m.group(3).strip()
        data["session_reset_at"] = m.group(4).strip()

    # Parse week line
    m = re.search(r'Current week:\s*(\d+)%\s*remaining\s*\((\d+)%\s*used\)\s*•\s*resets in\s*([\ddhm ]+)\s*\(([^)]+)\)', output)
    if m:
        data["week_remaining"] = int(m.group(1))
        data["week_used"] = int(m.group(2))
        data["week_reset_in"] = m.group(3).strip()
        data["week_reset_at"] = m.group(4).strip()

    return data


@app.get("/api/v1/usage/live")
def usage_live():
    """Live usage data from hermes usage command (account-level)."""
    return _parse_hermes_usage("default")


@app.get("/api/v1/usage/hourly")
def usage_hourly():
    """Token usage by hour of day (0-23) across last 7 days, per agent."""
    now = datetime.now(timezone.utc)
    seven_days_ago = (now - timedelta(days=7)).timestamp()
    tz_offset = -3  # BRT

    result = {"hours": list(range(24)), "agents": {}, "totals": [0] * 24}

    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        hourly = [0] * 24
        conn = get_db(profile)
        if not conn:
            result["agents"][profile] = {"name": cfg["name"], "hourly": hourly}
            continue
        try:
            rows = conn.execute(
                "SELECT started_at, input_tokens, output_tokens "
                "FROM sessions WHERE started_at >= ? ORDER BY started_at",
                (seven_days_ago,),
            ).fetchall()
            for row in rows:
                ts = row["started_at"]
                if not ts:
                    continue
                # Convert to local hour (BRT = UTC-3)
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                local_hour = (dt.hour + tz_offset) % 24
                tokens = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
                hourly[local_hour] += tokens
                result["totals"][local_hour] += tokens
        except Exception:
            pass
        finally:
            conn.close()
        result["agents"][profile] = {"name": cfg["name"], "hourly": hourly}

    return result


@app.get("/api/v1/usage/by-agent")
def usage_by_agent():
    """Token usage per agent for today and current week."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    # Week start = last Monday 00:00 UTC
    days_since_monday = now.weekday()
    week_start = (now - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    agents = []
    for cfg in AGENTS_CONFIG:
        profile = cfg["profile"]
        today_tokens = 0
        week_tokens = 0
        today_cost = 0.0
        week_cost = 0.0
        today_sessions = 0
        week_sessions = 0

        conn = get_db(profile)
        if conn:
            try:
                rows = conn.execute(
                    "SELECT started_at, input_tokens, output_tokens, estimated_cost_usd "
                    "FROM sessions WHERE started_at >= ?",
                    (week_start,),
                ).fetchall()
                for row in rows:
                    ts = row["started_at"]
                    if not ts:
                        continue
                    tok = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
                    cost = row["estimated_cost_usd"] or 0.0
                    week_tokens += tok
                    week_cost += cost
                    week_sessions += 1
                    if ts >= today_start:
                        today_tokens += tok
                        today_cost += cost
                        today_sessions += 1
            except Exception:
                pass
            finally:
                conn.close()

        agents.append({
            "name": cfg["name"],
            "profile": profile,
            "today": {"tokens": today_tokens, "cost": today_cost, "sessions": today_sessions},
            "week": {"tokens": week_tokens, "cost": week_cost, "sessions": week_sessions},
        })

    return agents


# --- DIMOB Conferência Endpoints ---

DIMOB_CONFERENCIA_PATH = Path("/home/hermes/cerebro-projetos-marketing/relatorios/dados/dimob-2026-novos-conferencia.json")
DIMOB_SNAPSHOT_PATH = Path("/tmp/dimob-2026-snapshot-atual.json")
DIMOB_ACORDOS_PATH = Path("/tmp/dimob-acordos-2026-completo.json")


def _load_json_safe(path: Path):
    """Load JSON file safely, return empty list/dict on error."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


@app.get("/api/v1/dimob/conferencia")
def dimob_conferencia():
    """Return DIMOB conference data with snapshot merge."""
    conferencia = _load_json_safe(DIMOB_CONFERENCIA_PATH)
    snapshot_raw = _load_json_safe(DIMOB_SNAPSHOT_PATH)

    # Build snapshot lookup
    snapshot = {}
    if isinstance(snapshot_raw, dict):
        for key, val in snapshot_raw.items():
            ct = val.get("contrato", "")
            cpf = val.get("locador_cpf", "")
            snapshot[f"{ct}_{cpf}"] = val
    elif isinstance(snapshot_raw, list):
        for val in snapshot_raw:
            ct = val.get("contrato", "")
            cpf = val.get("locador_cpf", "")
            snapshot[f"{ct}_{cpf}"] = val

    # Detect multi-locador (same CPF appears in multiple contracts)
    cpf_counts: dict[str, int] = {}
    for c in conferencia:
        cpf = c.get("locador_cpf", "")
        cpf_counts[cpf] = cpf_counts.get(cpf, 0) + 1

    # Enrich conferencia with snapshot and multi-locador
    for c in conferencia:
        key = f"{c.get('contrato', '')}_{c.get('locador_cpf', '')}"
        snap = snapshot.get(key)
        c["snapshot"] = snap
        c["multi_locador"] = cpf_counts.get(c.get("locador_cpf", ""), 0) > 1

        # Detect changes vs snapshot
        if snap:
            dimob = c.get("dimob_tela", {})
            changed = False
            for field in ["bruto", "comissao", "retido"]:
                if abs((dimob.get(field, 0) or 0) - (snap.get(field, 0) or 0)) > 0.01:
                    changed = True
                    break
            c["alterado"] = changed
        else:
            c["alterado"] = False

    # Stats
    total = len(conferencia)
    match_count = sum(1 for c in conferencia if c.get("status") == "✅")
    diverge_count = sum(1 for c in conferencia if c.get("status") == "⚠️")
    error_count = sum(1 for c in conferencia if c.get("status") == "❌")
    alterado_count = sum(1 for c in conferencia if c.get("alterado"))
    multi_count = sum(1 for c in conferencia if c.get("multi_locador"))

    return {
        "total": total,
        "match": match_count,
        "diverge": diverge_count,
        "error": error_count,
        "alterado": alterado_count,
        "multi_locador": multi_count,
        "ultima_conferencia": datetime.now(timezone.utc).isoformat(),
        "contratos": conferencia,
    }


@app.get("/api/v1/dimob/acordos")
def dimob_acordos():
    """Return DIMOB acordos data."""
    acordos = _load_json_safe(DIMOB_ACORDOS_PATH)
    if not isinstance(acordos, list):
        acordos = []

    # Group by contrato
    by_contrato: dict[str, list] = {}
    for a in acordos:
        ct = a.get("contrato", "?")
        by_contrato.setdefault(ct, []).append(a)

    total_valor = sum(a.get("valor", 0) or 0 for a in acordos)

    return {
        "total_acordos": len(acordos),
        "total_contratos": len(by_contrato),
        "total_valor": round(total_valor, 2),
        "acordos": acordos,
        "por_contrato": {k: len(v) for k, v in by_contrato.items()},
    }


@app.get("/dimob", response_class=HTMLResponse)
def dimob_page():
    """Serve the DIMOB conference panel."""
    return FileResponse(str(BASE_DIR / "static" / "dimob.html"))


# --- Chat Financeiro Endpoints ---

@app.get("/chat", response_class=HTMLResponse)
def chat_page():
    """Serve the chat interface for JI-Financeiro."""
    return FileResponse(str(BASE_DIR / "static" / "chat.html"))


def _clean_hermes_output(raw: str) -> str:
    """Extract just the bot reply from hermes CLI output, stripping noise."""
    lines = raw.split('\n')
    # Try to extract content between Hermes box markers
    in_box = False
    box_lines = []
    for line in lines:
        if '☤ Hermes' in line or '╭─' in line and 'Hermes' in line:
            in_box = True
            continue
        if in_box and line.strip().startswith('╰'):
            in_box = False
            continue
        if in_box:
            # Strip box border characters
            cleaned = line.strip()
            if cleaned.startswith('│'):
                cleaned = cleaned[1:]
            if cleaned.endswith('│'):
                cleaned = cleaned[:-1]
            box_lines.append(cleaned.strip())

    if box_lines:
        result = '\n'.join(box_lines).strip()
        if result:
            return result

    # Fallback: remove known noise patterns
    filtered = []
    skip_patterns = [
        'Query:', '⚠️', 'Normalized model', 'Initializing agent',
        '────', '╭─', '╰─', 'Resume this session',
        'hermes --resume', 'hermes -c', 'Session:', 'Title:',
        'Duration:', 'Messages:', 'Token usage:', 'Cost:',
    ]
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in skip_patterns):
            continue
        if '╭' in stripped or '╰' in stripped or '│' in stripped:
            cleaned = stripped.replace('│', '').strip()
            if cleaned:
                filtered.append(cleaned)
            continue
        filtered.append(stripped)

    return '\n'.join(filtered).strip()


@app.post("/api/v1/chat/send")
async def chat_send(request: Request):
    """Send a message to the JI-Financeiro bot and return the response (legacy sync endpoint)."""
    body = await request.json()
    message = body.get("message", "").strip()
    conversation_id = body.get("conversation_id", "")
    if not message:
        return JSONResponse({"error": "Mensagem vazia"}, status_code=400)

    # Build command with session context if available
    cmd = ["hermes", "--profile", "ji-financeiro", "chat", "-q", message]
    if conversation_id:
        session_name = f"chat-fin-{conversation_id}"
        cmd.extend(["--continue", session_name, "--create-if-missing"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        response_text = _clean_hermes_output(result.stdout)
        if not response_text and result.stderr.strip():
            response_text = "Desculpe, ocorreu um erro interno ao processar sua mensagem."
        if not response_text:
            response_text = "Sem resposta do assistente."
        return {"response": response_text, "conversation_id": conversation_id}
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"error": "O assistente demorou demais para responder. Tente novamente."},
            status_code=504,
        )
    except Exception as e:
        return JSONResponse({"error": f"Erro interno: {str(e)}"}, status_code=500)


# --- Streaming Chat Endpoint (V2 — SSE) ---

@app.post("/api/v1/chat/stream")
async def chat_stream(request: Request):
    """Stream bot response via Server-Sent Events (SSE).

    Uses hermes --format stream-json which outputs JSONL:
      {"type": "text", "text": "token..."}
      {"type": "result", "text": "full response", ...}
    """
    body = await request.json()
    message = body.get("message", "").strip()
    conversation_id = body.get("conversation_id", "")
    if not message:
        return JSONResponse({"error": "Mensagem vazia"}, status_code=400)

    # Build command
    cmd = [
        "hermes", "--profile", "ji-financeiro", "chat",
        "-q", message, "--format", "stream-json",
    ]
    if conversation_id:
        session_name = f"chat-fin-{conversation_id}"
        cmd.extend(["--continue", session_name, "--create-if-missing"])

    async def event_generator():
        """Yield SSE events from hermes subprocess stdout."""
        loop = asyncio.get_event_loop()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        full_text = ""
        try:
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=180
                    )
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'error', 'text': 'Timeout — o assistente demorou demais.'})}\n\n"
                    proc.kill()
                    break

                if not line:
                    break

                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue

                # Skip non-JSON lines (stderr leaks, warnings)
                if not raw.startswith("{"):
                    continue

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                evt_type = data.get("type", "")

                if evt_type == "text":
                    token = data.get("text", "")
                    full_text += token
                    yield f"data: {json.dumps({'type': 'text', 'text': token})}\n\n"

                elif evt_type == "result":
                    # Final message — send complete signal
                    final_text = data.get("text", full_text)
                    yield f"data: {json.dumps({'type': 'done', 'text': final_text})}\n\n"

                elif evt_type == "system":
                    # Init event — can send as status
                    yield f"data: {json.dumps({'type': 'status', 'text': 'Conectado ao assistente...'})}\n\n"

            # If process ended without a result event, send what we have
            await proc.wait()
            if full_text and not any(True for _ in []):  # always send done
                pass  # done already sent above in result handler

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- File Upload Endpoint ---

UPLOAD_DIR = Path("/home/hermes/cerebro-financeiro/inbox")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {".xlsx", ".pdf", ".png", ".jpg", ".jpeg", ".csv", ".txt", ".docx"}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB


@app.post("/api/v1/chat/upload")
async def chat_upload(request: Request, file: UploadFile = File(...)):
    """Upload a file to the financeiro inbox. Returns the saved path."""
    # Validate extension
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return JSONResponse(
            {"error": f"Tipo de arquivo não permitido: {ext}. Aceitos: {', '.join(sorted(ALLOWED_EXTENSIONS))}"},
            status_code=400,
        )

    # Read and validate size
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        return JSONResponse(
            {"error": f"Arquivo muito grande ({len(content) / 1024 / 1024:.1f}MB). Máximo: 10MB."},
            status_code=400,
        )

    # Save with date prefix
    date_prefix = datetime.now().strftime("%Y-%m-%d")
    safe_name = re.sub(r'[^\w\-_.]', '_', file.filename)
    dest_name = f"{date_prefix}-{safe_name}"
    dest_path = UPLOAD_DIR / dest_name

    # Avoid overwriting
    counter = 1
    while dest_path.exists():
        stem = Path(dest_name).stem
        dest_path = UPLOAD_DIR / f"{stem}_{counter}{ext}"
        counter += 1

    dest_path.write_bytes(content)

    return {
        "ok": True,
        "filename": dest_path.name,
        "path": str(dest_path),
        "size": len(content),
        "type": ext.lstrip("."),
    }


@app.get("/api/v1/chat/files/{filename}")
async def serve_uploaded_file(filename: str):
    """Serve an uploaded file (for image previews in chat)."""
    safe = Path(filename).name  # prevent path traversal
    fpath = UPLOAD_DIR / safe
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(fpath))


# --- Chat V2 Phase 3: Persistent History (SQLite) ---

CHAT_DB_PATH = BASE_DIR / "chat.db"


def _init_chat_db():
    """Create chat tables if they don't exist."""
    conn = sqlite3.connect(str(CHAT_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'Nova conversa',
            user_id TEXT NOT NULL,
            hermes_session TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_chat_conv_user ON chat_conversations(user_id);

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
            content TEXT NOT NULL,
            attachments_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(conversation_id);

        CREATE TABLE IF NOT EXISTS chat_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
            rating TEXT NOT NULL CHECK (rating IN ('like', 'dislike')),
            comment TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(message_id)
        );
    """)
    conn.close()


_init_chat_db()


def _init_notifications_table():
    """Create notifications table if missing."""
    conn = sqlite3.connect(str(CHAT_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'info' CHECK (type IN ('cron_complete', 'error', 'info')),
            title TEXT NOT NULL,
            body TEXT,
            read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_notif_read ON chat_notifications(read);
    """)
    conn.close()


_init_notifications_table()


def _get_chat_db():
    """Open a read-write connection to the chat DB."""
    conn = sqlite3.connect(str(CHAT_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@app.get("/api/v1/chat/conversations")
def list_conversations(request: Request):
    """List conversations for the current user (admin sees all, viewer sees own)."""
    sess = request.state.session
    user = sess["user"]
    role = sess["role"]
    filter_user = request.query_params.get("filter_user", "")

    conn = _get_chat_db()
    try:
        if role == "admin" and filter_user:
            rows = conn.execute(
                "SELECT * FROM chat_conversations WHERE user_id = ? ORDER BY updated_at DESC",
                (filter_user,),
            ).fetchall()
        elif role == "admin":
            rows = conn.execute(
                "SELECT * FROM chat_conversations ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chat_conversations WHERE user_id = ? ORDER BY updated_at DESC",
                (user,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/v1/chat/conversations")
async def create_conversation(request: Request):
    """Create a new conversation. Also syncs the previous active conversation."""
    sess = request.state.session
    body = await request.json()
    conv_id = body.get("id", uuid.uuid4().hex[:12])
    title = body.get("title", "Nova conversa")
    prev_conv_id = body.get("previous_conversation_id")

    # Sync previous conversation to cerebro if it had messages
    if prev_conv_id:
        try:
            sync_to_cerebro(prev_conv_id)
            _conv_last_activity.pop(prev_conv_id, None)
        except Exception:
            pass

    conn = _get_chat_db()
    try:
        hermes_session = f"chat-fin-{conv_id}"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO chat_conversations (id, title, user_id, hermes_session, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, title, sess["user"], hermes_session, now, now),
        )
        conn.commit()
        return {"id": conv_id, "title": title, "hermes_session": hermes_session}
    finally:
        conn.close()


@app.patch("/api/v1/chat/conversations/{conv_id}")
async def update_conversation(conv_id: str, request: Request):
    """Update conversation title."""
    sess = request.state.session
    body = await request.json()
    title = body.get("title", "")

    conn = _get_chat_db()
    try:
        # Check ownership (admin can edit any)
        row = conn.execute("SELECT user_id FROM chat_conversations WHERE id = ?", (conv_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if sess["role"] != "admin" and row["user_id"] != sess["user"]:
            raise HTTPException(status_code=403, detail="Forbidden")

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE chat_conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, conv_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/v1/chat/conversations/{conv_id}")
def delete_conversation_api(conv_id: str, request: Request):
    """Delete a conversation and its messages."""
    sess = request.state.session

    conn = _get_chat_db()
    try:
        row = conn.execute("SELECT user_id FROM chat_conversations WHERE id = ?", (conv_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if sess["role"] != "admin" and row["user_id"] != sess["user"]:
            raise HTTPException(status_code=403, detail="Forbidden")

        conn.execute("DELETE FROM chat_messages WHERE conversation_id = ?", (conv_id,))
        conn.execute("DELETE FROM chat_feedback WHERE message_id NOT IN (SELECT id FROM chat_messages)")
        conn.execute("DELETE FROM chat_conversations WHERE id = ?", (conv_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/chat/conversations/{conv_id}/messages")
def get_conversation_messages(conv_id: str, request: Request):
    """Get all messages for a conversation."""
    sess = request.state.session

    conn = _get_chat_db()
    try:
        row = conn.execute("SELECT user_id FROM chat_conversations WHERE id = ?", (conv_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if sess["role"] != "admin" and row["user_id"] != sess["user"]:
            raise HTTPException(status_code=403, detail="Forbidden")

        messages = conn.execute(
            """SELECT m.id, m.conversation_id, m.role, m.content, m.attachments_json, m.created_at,
                      f.rating as feedback_rating
               FROM chat_messages m
               LEFT JOIN chat_feedback f ON f.message_id = m.id
               WHERE m.conversation_id = ?
               ORDER BY m.created_at ASC""",
            (conv_id,),
        ).fetchall()

        result = []
        for m in messages:
            msg = dict(m)
            if msg["attachments_json"]:
                try:
                    msg["attachment"] = json.loads(msg["attachments_json"])
                except Exception:
                    msg["attachment"] = None
            else:
                msg["attachment"] = None
            del msg["attachments_json"]
            result.append(msg)
        return result
    finally:
        conn.close()


@app.post("/api/v1/chat/messages")
async def save_message(request: Request):
    """Save a message to the database. Returns the message ID."""
    sess = request.state.session
    body = await request.json()
    conv_id = body.get("conversation_id")
    role = body.get("role", "user")
    content = body.get("content", "")
    attachment = body.get("attachment")

    if not conv_id or not content:
        return JSONResponse({"error": "Missing conversation_id or content"}, status_code=400)

    conn = _get_chat_db()
    try:
        # Verify conversation exists and user has access
        row = conn.execute("SELECT user_id FROM chat_conversations WHERE id = ?", (conv_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if sess["role"] != "admin" and row["user_id"] != sess["user"]:
            raise HTTPException(status_code=403, detail="Forbidden")

        now = datetime.now(timezone.utc).isoformat()
        attachments_json = json.dumps(attachment) if attachment else None
        cursor = conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, attachments_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (conv_id, role, content, attachments_json, now),
        )
        msg_id = cursor.lastrowid

        # Update conversation's updated_at
        conn.execute("UPDATE chat_conversations SET updated_at = ? WHERE id = ?", (now, conv_id))

        # Auto-title: use first user message
        if role == "user":
            msg_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM chat_messages WHERE conversation_id = ? AND role = 'user'",
                (conv_id,),
            ).fetchone()["cnt"]
            if msg_count == 1:
                title = content[:50] + ("…" if len(content) > 50 else "")
                conn.execute("UPDATE chat_conversations SET title = ? WHERE id = ?", (title, conv_id))

        conn.commit()
        # Track activity for auto-sync
        _conv_last_activity[conv_id] = time.time()
        return {"id": msg_id, "created_at": now}
    finally:
        conn.close()


@app.post("/api/v1/chat/feedback")
async def save_feedback(request: Request):
    """Save like/dislike feedback for a message."""
    body = await request.json()
    message_id = body.get("message_id")
    rating = body.get("rating")
    comment = body.get("comment", "")

    if not message_id or rating not in ("like", "dislike"):
        return JSONResponse({"error": "Invalid message_id or rating"}, status_code=400)

    conn = _get_chat_db()
    try:
        # Verify message exists
        row = conn.execute("SELECT id FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Message not found")

        conn.execute(
            "INSERT OR REPLACE INTO chat_feedback (message_id, rating, comment, created_at) VALUES (?, ?, ?, datetime('now'))",
            (message_id, rating, comment),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/chat/search")
def search_conversations(request: Request):
    """Search messages by text content."""
    sess = request.state.session
    query = request.query_params.get("q", "").strip()
    if not query or len(query) < 2:
        return []

    conn = _get_chat_db()
    try:
        search_term = f"%{query}%"
        if sess["role"] == "admin":
            rows = conn.execute(
                """SELECT m.id, m.conversation_id, m.role, m.content, m.created_at,
                          c.title as conv_title, c.user_id
                   FROM chat_messages m
                   JOIN chat_conversations c ON c.id = m.conversation_id
                   WHERE m.content LIKE ?
                   ORDER BY m.created_at DESC LIMIT 30""",
                (search_term,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT m.id, m.conversation_id, m.role, m.content, m.created_at,
                          c.title as conv_title, c.user_id
                   FROM chat_messages m
                   JOIN chat_conversations c ON c.id = m.conversation_id
                   WHERE m.content LIKE ? AND c.user_id = ?
                   ORDER BY m.created_at DESC LIMIT 30""",
                (search_term, sess["user"]),
            ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            # Create snippet around the match
            content = d["content"]
            idx = content.lower().find(query.lower())
            start = max(0, idx - 40)
            end = min(len(content), idx + len(query) + 40)
            snippet = ("…" if start > 0 else "") + content[start:end] + ("…" if end < len(content) else "")
            d["snippet"] = snippet
            d["content"] = content[:200]  # limit content in search results
            results.append(d)
        return results
    finally:
        conn.close()


@app.get("/api/v1/chat/users")
def list_chat_users(request: Request):
    """List users who have conversations (for admin filter dropdown)."""
    sess = request.state.session
    if sess["role"] != "admin":
        return []

    conn = _get_chat_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_id, COUNT(*) as conv_count FROM chat_conversations GROUP BY user_id ORDER BY user_id"
        ).fetchall()
        return [{"user_id": r["user_id"], "conv_count": r["conv_count"]} for r in rows]
    finally:
        conn.close()


# --- Chat V2 Phase 3: Cerebro Sync ---

CEREBRO_CHAT_DIR = Path("/home/hermes/cerebro-financeiro/inbox/chat")
CEREBRO_CHAT_DIR.mkdir(parents=True, exist_ok=True)

# Track last activity per conversation for auto-sync
_conv_last_activity: dict[str, float] = {}


def sync_to_cerebro(conv_id: str) -> str | None:
    """Generate a .md summary of a conversation and save to cerebro-financeiro inbox."""
    conn = _get_chat_db()
    try:
        conv = conn.execute("SELECT * FROM chat_conversations WHERE id = ?", (conv_id,)).fetchone()
        if not conv:
            return None

        messages = conn.execute(
            "SELECT role, content, attachments_json, created_at FROM chat_messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conv_id,),
        ).fetchall()

        if not messages:
            return None

        title = conv["title"] or "Nova conversa"
        user_id = conv["user_id"]
        user_name = USERS.get(user_id, {}).get("name", user_id)
        created = conv["created_at"][:10] if conv["created_at"] else datetime.now().strftime("%Y-%m-%d")

        # Build markdown
        safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '-')[:40]
        filename = f"{created}-{safe_title}.md"
        filepath = CEREBRO_CHAT_DIR / filename

        # Collect files
        arquivos = []
        msg_lines = []
        for m in messages:
            role_label = "👤 Usuário" if m["role"] == "user" else "🤖 Assistente"
            ts = m["created_at"][11:16] if m["created_at"] and len(m["created_at"]) > 16 else ""
            content_preview = m["content"][:500] + ("…" if len(m["content"]) > 500 else "")
            msg_lines.append(f"**{role_label}** ({ts}):\n{content_preview}\n")

            if m["attachments_json"]:
                try:
                    att = json.loads(m["attachments_json"])
                    if att and isinstance(att, dict):
                        arquivos.append(f"- {att.get('filename', '?')} ({att.get('type', '?')}, {att.get('size', 0)} bytes) → `{att.get('path', '?')}`")
                except Exception:
                    pass

        # Build summary section (first user msg + last assistant msg)
        first_user = next((m["content"][:200] for m in messages if m["role"] == "user"), "")
        last_assistant = ""
        for m in reversed(list(messages)):
            if m["role"] == "assistant":
                last_assistant = m["content"][:200]
                break

        md = f"""# {title}

| Campo | Valor |
|-------|-------|
| **Data** | {created} |
| **Participante** | {user_name} ({user_id}) |
| **Conversa ID** | {conv_id} |
| **Total mensagens** | {len(list(messages))} |

## Resumo

**Pergunta inicial:** {first_user}

**Última resposta:** {last_assistant}

## Mensagens

{chr(10).join(msg_lines)}

## Arquivos

{chr(10).join(arquivos) if arquivos else "_Nenhum arquivo enviado._"}

---
_Sincronizado automaticamente pelo Chat V2 em {datetime.now(timezone.utc).isoformat()}_
"""
        filepath.write_text(md, encoding="utf-8")
        return str(filepath)

    finally:
        conn.close()


@app.post("/api/v1/chat/conversations/{conv_id}/sync")
def sync_conversation(conv_id: str, request: Request):
    """Manually sync a conversation to cerebro-financeiro."""
    sess = request.state.session
    conn = _get_chat_db()
    try:
        row = conn.execute("SELECT user_id FROM chat_conversations WHERE id = ?", (conv_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if sess["role"] != "admin" and row["user_id"] != sess["user"]:
            raise HTTPException(status_code=403, detail="Forbidden")
    finally:
        conn.close()

    path = sync_to_cerebro(conv_id)
    if path:
        return {"ok": True, "path": path}
    return JSONResponse({"error": "No messages to sync"}, status_code=400)


def _check_inactive_syncs():
    """Background check: sync conversations inactive for 5+ minutes."""
    now = time.time()
    to_sync = []
    for cid, last_ts in list(_conv_last_activity.items()):
        if now - last_ts >= 300:  # 5 minutes
            to_sync.append(cid)
            del _conv_last_activity[cid]
    for cid in to_sync:
        try:
            sync_to_cerebro(cid)
        except Exception:
            pass


def _start_sync_checker():
    """Start background thread that checks for inactive conversations."""
    def _checker():
        while True:
            time.sleep(60)
            try:
                _check_inactive_syncs()
            except Exception:
                pass
    t = threading.Thread(target=_checker, daemon=True)
    t.start()


_start_sync_checker()


# --- Notifications Endpoints (Phase 4) ---

@app.get("/api/v1/chat/notifications")
def list_notifications(request: Request):
    """List unread + recent notifications."""
    conn = _get_chat_db()
    try:
        rows = conn.execute(
            "SELECT id, type, title, body, read, created_at FROM chat_notifications ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/v1/chat/notifications/unread-count")
def notification_unread_count(request: Request):
    """Return count of unread notifications."""
    conn = _get_chat_db()
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM chat_notifications WHERE read = 0").fetchone()
        return {"count": row["cnt"]}
    finally:
        conn.close()


@app.post("/api/v1/chat/notifications/{notif_id}/read")
def mark_notification_read(notif_id: int, request: Request):
    """Mark a single notification as read."""
    conn = _get_chat_db()
    try:
        conn.execute("UPDATE chat_notifications SET read = 1 WHERE id = ?", (notif_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/v1/chat/notifications/read-all")
def mark_all_notifications_read(request: Request):
    """Mark all notifications as read."""
    conn = _get_chat_db()
    try:
        conn.execute("UPDATE chat_notifications SET read = 1 WHERE read = 0")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/v1/chat/notifications")
async def create_notification(request: Request):
    """Create a notification (for cron results, errors, etc.)."""
    body = await request.json()
    notif_type = body.get("type", "info")
    title = body.get("title", "")
    notif_body = body.get("body", "")
    if not title:
        return JSONResponse({"error": "Title required"}, status_code=400)
    if notif_type not in ("cron_complete", "error", "info"):
        notif_type = "info"

    conn = _get_chat_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "INSERT INTO chat_notifications (type, title, body, created_at) VALUES (?, ?, ?, ?)",
            (notif_type, title, notif_body, now),
        )
        conn.commit()
        return {"id": cursor.lastrowid, "ok": True}
    finally:
        conn.close()


# --- Static files & frontend ---
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8091)
