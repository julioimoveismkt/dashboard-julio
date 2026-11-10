"""Dashboard de Gestão de Agentes — Julio Imóveis (Fase 1 + Fase 2 + Fase 3)"""

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

from fastapi import FastAPI, Request, Response, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

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


# --- Static files & frontend ---
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8091)
