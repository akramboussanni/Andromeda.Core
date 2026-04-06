import hmac
import os
import secrets
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import database as db
import logs_db
from admin_state import PENDING_COMMANDS
import services.party_service as ps

router = APIRouter()

# ---------------------------------------------------------------------------
# Config — set ADMIN_PASSWORD in .env
# ---------------------------------------------------------------------------
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_TOKEN", "")
SESSION_TTL = 8 * 3600          # 8 hours
_RATE_LIMIT_MAX = 5             # max attempts
_RATE_LIMIT_WINDOW = 60         # per N seconds

# ---------------------------------------------------------------------------
# In-memory session store  {token: expires_at}
# ---------------------------------------------------------------------------
_sessions: dict[str, float] = {}


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    _purge_sessions()
    return token


def _valid_session(token: Optional[str]) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if exp is None or time.time() > exp:
        _sessions.pop(token, None)
        return False
    return True


def _purge_sessions():
    now = time.time()
    stale = [k for k, v in _sessions.items() if now > v]
    for k in stale:
        del _sessions[k]


# ---------------------------------------------------------------------------
# Login rate limiter  {ip: [timestamp, ...]}
# ---------------------------------------------------------------------------
_login_attempts: dict[str, list] = defaultdict(list)


def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < _RATE_LIMIT_WINDOW]
    _login_attempts[ip] = attempts
    if len(attempts) >= _RATE_LIMIT_MAX:
        return True
    _login_attempts[ip].append(now)
    return False


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _require_session(session: Optional[str]):
    """Raise a redirect to /admin/login if the session is invalid."""
    if not _valid_session(session):
        raise _login_redirect()


def _login_redirect():
    from fastapi import HTTPException
    # We raise a real redirect rather than HTTPException so headers work
    return _LoginRedirect()


class _LoginRedirect(Exception):
    pass


# We need a middleware-style approach — easier to just check inline and return early.
def _check_session(session: Optional[str]) -> bool:
    return _valid_session(session)


def _set_session_cookie(response, token: str):
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="strict",
        path="/admin",
        max_age=SESSION_TTL,
    )


def _clear_session_cookie(response):
    response.delete_cookie(key="admin_session", path="/admin")


# ---------------------------------------------------------------------------
# Login page HTML
# ---------------------------------------------------------------------------

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Andromeda Admin — Login</title>
<style>
  :root { --bg:#0f1117; --surface:#1a1d27; --border:#2a2d3e; --text:#e2e8f0;
          --text-muted:#64748b; --accent:#6366f1; --danger:#ef4444; --radius:10px; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:'Inter','Segoe UI',system-ui,sans-serif; background:var(--bg);
         color:var(--text); display:flex; align-items:center; justify-content:center;
         min-height:100vh; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
          padding:40px 36px; width:100%; max-width:380px; }
  h1 { font-size:22px; font-weight:700; color:var(--accent); margin-bottom:6px; }
  .sub { color:var(--text-muted); font-size:13px; margin-bottom:28px; }
  label { display:block; font-size:12px; font-weight:600; color:var(--text-muted);
          text-transform:uppercase; letter-spacing:.5px; margin-bottom:6px; }
  input[type=password] {
    width:100%; background:#0f1117; border:1px solid var(--border);
    border-radius:8px; padding:10px 14px; color:var(--text); font-size:15px;
    outline:none; margin-bottom:20px; transition:border-color .15s;
  }
  input[type=password]:focus { border-color:var(--accent); }
  button { width:100%; background:var(--accent); color:white; border:none;
           border-radius:8px; padding:11px; font-size:15px; font-weight:600;
           cursor:pointer; transition:opacity .15s; }
  button:hover { opacity:.88; }
  .error { color:var(--danger); font-size:13px; margin-top:-12px; margin-bottom:16px;
           display:none; }
  .error.show { display:block; }
  .lock { font-size:40px; margin-bottom:16px; }
</style>
</head>
<body>
<div class="card">
  <div class="lock">🔒</div>
  <h1>Andromeda Admin</h1>
  <p class="sub">Sign in to access the control panel.</p>
  <form method="POST" action="/admin/login">
    <label for="pw">Password</label>
    <input type="password" id="pw" name="password" autofocus autocomplete="current-password" placeholder="••••••••">
    <p class="error {err_class}">{err_msg}</p>
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Admin panel HTML
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Andromeda Admin</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --surface2: #222537; --border: #2a2d3e;
    --text: #e2e8f0; --text-muted: #64748b; --accent: #6366f1; --accent-hover: #818cf8;
    --danger: #ef4444; --danger-hover: #f87171; --success: #22c55e;
    --warning: #f59e0b; --info: #3b82f6; --radius: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter','Segoe UI',system-ui,sans-serif; background: var(--bg);
         color: var(--text); display: flex; height: 100vh; overflow: hidden; font-size: 14px; }

  /* Sidebar */
  .sidebar { width: 220px; background: var(--surface); border-right: 1px solid var(--border);
             display: flex; flex-direction: column; flex-shrink: 0; }
  .sidebar-logo { padding: 20px 20px 16px; border-bottom: 1px solid var(--border); }
  .sidebar-logo h1 { font-size: 18px; font-weight: 700; color: var(--accent); letter-spacing: -.5px; }
  .sidebar-logo span { font-size: 11px; color: var(--text-muted); display: block; margin-top: 2px; }
  .nav { flex: 1; padding: 12px 8px; display: flex; flex-direction: column; gap: 2px; }
  .nav-item { display: flex; align-items: center; gap: 10px; padding: 9px 12px;
              border-radius: var(--radius); cursor: pointer; color: var(--text-muted);
              font-weight: 500; transition: all .15s; border: none; background: transparent;
              width: 100%; text-align: left; font-size: 14px; }
  .nav-item:hover { background: var(--surface2); color: var(--text); }
  .nav-item.active { background: var(--accent); color: white; }
  .nav-item .icon { font-size: 16px; width: 18px; text-align: center; }
  .sidebar-footer { padding: 16px; border-top: 1px solid var(--border); font-size: 11px; color: var(--text-muted); }
  .logout-btn { display: flex; align-items: center; gap: 8px; color: var(--text-muted);
                cursor: pointer; background: none; border: none; font-size: 12px;
                padding: 6px 0; transition: color .15s; }
  .logout-btn:hover { color: var(--danger); }

  /* Main */
  .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  .topbar { padding: 14px 24px; background: var(--surface); border-bottom: 1px solid var(--border);
            display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
  .topbar-title { font-size: 16px; font-weight: 600; }
  .topbar-meta { font-size: 12px; color: var(--text-muted); }
  .content { flex: 1; overflow-y: auto; padding: 24px; }

  /* Pages */
  .page { display: none; } .page.active { display: block; }

  /* Stats */
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr));
                gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border);
               border-radius: var(--radius); padding: 18px 20px; }
  .stat-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase;
                letter-spacing: .5px; margin-bottom: 8px; }
  .stat-value { font-size: 28px; font-weight: 700; }
  .stat-value.accent { color: var(--accent); }
  .stat-value.success { color: var(--success); }
  .stat-value.warning { color: var(--warning); }

  /* Cards */
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: var(--radius); overflow: hidden; }
  .card-header { padding: 14px 18px; border-bottom: 1px solid var(--border);
                 display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .card-title { font-weight: 600; font-size: 14px; }
  .card-body { padding: 18px; }

  /* Table */
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 14px; font-size: 11px; font-weight: 600;
       text-transform: uppercase; letter-spacing: .5px; color: var(--text-muted);
       border-bottom: 1px solid var(--border); background: var(--surface2); white-space: nowrap; }
  td { padding: 10px 14px; border-bottom: 1px solid var(--border); color: var(--text); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(99,102,241,.04); }
  .mono { font-family: 'Consolas',monospace; font-size: 12px; color: var(--text-muted); }

  /* Badges */
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px;
           font-weight: 600; text-transform: uppercase; letter-spacing: .3px; white-space: nowrap; }
  .badge-tcp     { background:#7c3aed22; color:#a78bfa; border:1px solid #7c3aed44; }
  .badge-api     { background:#d9770622; color:#fb923c; border:1px solid #d9770644; }
  .badge-lobby   { background:#1d4ed822; color:#60a5fa; border:1px solid #1d4ed844; }
  .badge-error   { background:#dc262622; color:#f87171; border:1px solid #dc262644; }
  .badge-warning { background:#d9770622; color:#fbbf24; border:1px solid #d9770644; }
  .badge-success { background:#16a34a22; color:#4ade80; border:1px solid #16a34a44; }
  .badge-action  { background:#0891b222; color:#22d3ee; border:1px solid #0891b244; }
  .badge-server  { background:#16a34a22; color:#4ade80; border:1px solid #16a34a44; }
  .badge-info    { background:#33333322; color:#94a3b8; border:1px solid #44444444; }
  .badge-ready   { background:#16a34a22; color:#4ade80; border:1px solid #16a34a44; }
  .badge-pending { background:#d9770622; color:#fbbf24; border:1px solid #d9770644; }

  /* Logs */
  .log-controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .tag-filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .tag-btn { padding: 4px 12px; border-radius: 20px; border: 1px solid var(--border);
             background: var(--surface2); color: var(--text-muted); cursor: pointer;
             font-size: 12px; font-weight: 600; transition: all .15s; }
  .tag-btn:hover { color: var(--text); border-color: var(--accent); }
  .tag-btn.active { background: var(--accent); color: white; border-color: var(--accent); }
  #log-stream { height: 520px; overflow-y: auto; background: #0a0c12;
                border-radius: var(--radius); padding: 12px;
                font-family: 'Consolas','Monaco',monospace; font-size: 13px; line-height: 1.6; }
  .log-entry { display: flex; gap: 10px; align-items: baseline; padding: 2px 0;
               border-bottom: 1px solid #ffffff06; }
  .log-entry:hover { background: #ffffff05; }
  .log-ts { color: #475569; flex-shrink: 0; font-size: 11px; }
  .log-msg { color: #cbd5e1; word-break: break-all; }
  .log-msg.error { color: #fca5a5; } .log-msg.warning { color: #fcd34d; }
  .log-msg.success { color: #86efac; } .log-msg.action { color: #67e8f9; }

  /* Forms */
  .form-group { margin-bottom: 16px; }
  .form-label { display: block; margin-bottom: 6px; font-size: 13px; font-weight: 500; color: var(--text-muted); }
  input[type=text], textarea, select { width: 100%; background: var(--surface2);
    border: 1px solid var(--border); border-radius: var(--radius); padding: 9px 12px;
    color: var(--text); font-size: 14px; outline: none; transition: border-color .15s; font-family: inherit; }
  input[type=text]:focus, textarea:focus { border-color: var(--accent); }
  textarea { resize: vertical; min-height: 80px; }
  .search-input { width: 220px !important; }

  /* Buttons */
  .btn { padding: 9px 18px; border-radius: var(--radius); border: none; cursor: pointer;
         font-size: 14px; font-weight: 600; transition: all .15s; display: inline-flex;
         align-items: center; gap: 6px; }
  .btn-primary { background: var(--accent); color: white; }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-danger { background: var(--danger); color: white; }
  .btn-danger:hover { background: var(--danger-hover); }
  .btn-ghost { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }
  .btn-ghost:hover { border-color: var(--accent); color: var(--accent); }
  .btn-sm { padding: 5px 12px; font-size: 12px; }

  /* Action cards */
  .action-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .action-card { background: var(--surface); border: 1px solid var(--border);
                 border-radius: var(--radius); padding: 24px; }
  .action-card h3 { font-size: 16px; font-weight: 600; margin-bottom: 6px; }
  .action-card p { color: var(--text-muted); font-size: 13px; margin-bottom: 18px; line-height: 1.5; }
  .action-card.danger-zone { border-color: #ef444433; }
  .action-card.danger-zone h3 { color: var(--danger); }

  /* Toast */
  #toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface2);
           border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 20px;
           font-size: 14px; font-weight: 500; z-index: 9999;
           transform: translateY(100px); opacity: 0; transition: all .25s ease; max-width: 320px; }
  #toast.show { transform: translateY(0); opacity: 1; }
  #toast.success { border-color: #22c55e55; color: var(--success); }
  #toast.error   { border-color: #ef444455; color: var(--danger); }

  .empty-state { text-align: center; padding: 48px; color: var(--text-muted); }
  .empty-state .icon { font-size: 36px; margin-bottom: 12px; }

  /* Log filter bar */
  .filter-group { display: flex; flex-direction: column; gap: 4px; }
  .filter-label { font-size: 11px; font-weight: 600; color: var(--text-muted);
                  text-transform: uppercase; letter-spacing: .4px; }
  .filter-select, .filter-input {
    background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 6px 10px; color: var(--text); font-size: 13px; outline: none;
    transition: border-color .15s; min-width: 120px;
  }
  .filter-select:focus, .filter-input:focus { border-color: var(--accent); }

  /* Log table */
  #log-table td { font-size: 13px; max-width: 400px;
                  overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  #log-table tr:hover td { background: rgba(99,102,241,.06); cursor: pointer; }
</style>
</head>
<body>

<aside class="sidebar">
  <div class="sidebar-logo">
    <h1>⚡ Andromeda</h1>
    <span>Admin Panel</span>
  </div>
  <nav class="nav">
    <button class="nav-item active" onclick="navigate('overview', this)"><span class="icon">📊</span><span>Overview</span></button>
    <button class="nav-item" onclick="navigate('logs', this)"><span class="icon">📋</span><span>Logs</span></button>
    <button class="nav-item" onclick="navigate('players', this)"><span class="icon">👥</span><span>Players</span></button>
    <button class="nav-item" onclick="navigate('sessions', this)"><span class="icon">🎮</span><span>Sessions</span></button>
    <button class="nav-item" onclick="navigate('actions', this)"><span class="icon">⚡</span><span>Actions</span></button>
  </nav>
  <div class="sidebar-footer">
    <button class="logout-btn" onclick="logout()">🚪 Sign out</button>
  </div>
</aside>

<div class="main">
  <div class="topbar">
    <div class="topbar-title" id="page-title">Overview</div>
    <div class="topbar-meta" id="topbar-meta">Loading...</div>
  </div>
  <div class="content">

    <!-- Overview -->
    <div class="page active" id="page-overview">
      <div class="stats-grid">
        <div class="stat-card"><div class="stat-label">Total Players</div><div class="stat-value accent" id="stat-players">—</div></div>
        <div class="stat-card"><div class="stat-label">Active Sessions</div><div class="stat-value success" id="stat-sessions">—</div></div>
        <div class="stat-card"><div class="stat-label">Log Entries</div><div class="stat-value" id="stat-logs">—</div></div>
        <div class="stat-card"><div class="stat-label">Errors Logged</div><div class="stat-value" style="color:var(--danger)" id="stat-errors">—</div></div>
        <div class="stat-card"><div class="stat-label">Total Games Played</div><div class="stat-value warning" id="stat-games">—</div></div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Recent Log Activity</span></div>
        <div id="overview-logs" style="max-height:320px;overflow-y:auto;padding:12px;font-family:monospace;font-size:12px;background:#0a0c12;"></div>
      </div>
    </div>

    <!-- Logs -->
    <div class="page" id="page-logs">
      <!-- Filter bar -->
      <div class="card" style="margin-bottom:16px;">
        <div class="card-body" style="padding:14px 18px;">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
            <div class="filter-group">
              <label class="filter-label">Level</label>
              <select id="f-level" onchange="queryLogs(true)" class="filter-select">
                <option value="">All</option>
                <option value="error">⚠ Error</option>
                <option value="exception">💥 Exception</option>
                <option value="warning">⚡ Warning</option>
                <option value="info">ℹ Info</option>
              </select>
            </div>
            <div class="filter-group">
              <label class="filter-label">Player (Steam ID)</label>
              <select id="f-steam" onchange="queryLogs(true)" class="filter-select" style="max-width:200px;">
                <option value="">All players</option>
              </select>
            </div>
            <div class="filter-group">
              <label class="filter-label">Session ID</label>
              <input type="text" id="f-session" class="filter-input" placeholder="e.g. QVDSZ" onkeydown="if(event.key==='Enter')queryLogs(true)">
            </div>
            <div class="filter-group" style="flex:1;min-width:180px;">
              <label class="filter-label">Search</label>
              <input type="text" id="f-search" class="filter-input" placeholder="Search messages, errors, URLs..." onkeydown="if(event.key==='Enter')queryLogs(true)">
            </div>
            <div style="display:flex;gap:8px;margin-top:18px;">
              <button class="btn btn-primary btn-sm" onclick="queryLogs(true)">🔍 Search</button>
              <button class="btn btn-ghost btn-sm" onclick="clearFilters()">✕ Clear</button>
              <button class="btn btn-ghost btn-sm" onclick="clearLogs()" style="color:var(--danger)">🗑 Purge all</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Log table -->
      <div class="card">
        <div class="card-header">
          <span class="card-title">Log Entries</span>
          <div style="display:flex;align-items:center;gap:12px;">
            <span id="log-count" style="font-size:12px;color:var(--text-muted)"></span>
            <button class="btn btn-ghost btn-sm" id="new-entries-btn" style="display:none;color:var(--accent)" onclick="queryLogs(true)">↻ New entries</button>
          </div>
        </div>
        <div class="table-wrap">
          <table id="log-table">
            <thead>
              <tr>
                <th style="width:130px">Time</th>
                <th style="width:90px">Level</th>
                <th style="width:140px">Steam ID</th>
                <th style="width:80px">Session</th>
                <th style="width:110px">Type</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody id="log-tbody">
              <tr><td colspan="6"><div class="empty-state"><div class="icon">⏳</div>Loading...</div></td></tr>
            </tbody>
          </table>
        </div>
        <div style="padding:12px 18px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;">
          <button class="btn btn-ghost btn-sm" id="load-more-btn" onclick="loadMore()" style="display:none">Load older entries</button>
          <span></span>
        </div>
      </div>

      <!-- Detail panel -->
      <div id="log-detail" style="display:none;margin-top:16px;">
        <div class="card">
          <div class="card-header">
            <span class="card-title">Entry Detail</span>
            <button class="btn btn-ghost btn-sm" onclick="document.getElementById('log-detail').style.display='none'">✕ Close</button>
          </div>
          <div class="card-body" style="font-family:monospace;font-size:12px;">
            <div id="log-detail-content"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- Players -->
    <div class="page" id="page-players">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Registered Players</span>
          <input type="text" class="search-input" id="player-search" placeholder="Search steam ID..." oninput="filterPlayers()">
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Steam ID</th><th>Rank</th><th>Credits</th><th>Total Games</th><th>Backer</th><th>Joined</th><th>Last Seen</th></tr></thead>
            <tbody id="players-tbody"><tr><td colspan="7"><div class="empty-state"><div class="icon">⏳</div>Loading...</div></td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Sessions -->
    <div class="page" id="page-sessions">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Active Game Sessions</span>
          <button class="btn btn-ghost btn-sm" onclick="loadSessions()">↻ Refresh</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Session ID</th><th>Name</th><th>Region</th><th>Status</th><th>Players</th><th>Public</th><th>Host</th><th>IP:Port</th></tr></thead>
            <tbody id="sessions-tbody"><tr><td colspan="8"><div class="empty-state"><div class="icon">⏳</div>Loading...</div></td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Actions -->
    <div class="page" id="page-actions">
      <div class="action-grid">
        <div class="action-card">
          <h3>📢 Broadcast Message</h3>
          <p>Send a message that will appear as an in-game notification for all connected players on their next poll (within ~5 seconds).</p>
          <div class="form-group">
            <label class="form-label">Message</label>
            <textarea id="broadcast-msg" placeholder="Enter your message..."></textarea>
          </div>
          <button class="btn btn-primary" onclick="sendBroadcast()">📢 Send Broadcast</button>
        </div>
        <div class="action-card danger-zone">
          <h3>🔴 Force Exit All Games</h3>
          <p>Immediately force-quit the game client for all connected players. Use this only in emergencies — players will lose any unsaved progress.</p>
          <p style="color:var(--danger);font-size:12px;margin-top:-10px;margin-bottom:18px;">⚠️ This action cannot be undone.</p>
          <button class="btn btn-danger" onclick="confirmForceExit()">🔴 Force Exit Everyone</button>
        </div>
      </div>
      <div class="card" style="margin-top:20px;">
        <div class="card-header"><span class="card-title">Command History</span></div>
        <div class="card-body">
          <div id="cmd-history" style="font-family:monospace;font-size:12px;color:var(--text-muted);min-height:60px;">No commands sent this session.</div>
        </div>
      </div>
    </div>

  </div>
</div>

<div id="toast"></div>

<script>
  // All fetch calls use same-origin cookies automatically — no token in URL.
  async function apiFetch(path, opts = {}) {
    const r = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    if (r.status === 401) { window.location.href = '/admin/login'; throw new Error('Unauthenticated'); }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  async function logout() {
    await fetch('/admin/logout', { method: 'POST', credentials: 'same-origin' });
    window.location.href = '/admin/login';
  }

  // Toast
  function toast(msg, type = 'success') {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.className = `show ${type}`;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.className = ''; }, 3000);
  }

  // Navigation
  let currentPage = 'overview';
  const titles = { overview:'Overview', logs:'Log Stream', players:'Players', sessions:'Game Sessions', actions:'Admin Actions' };
  function navigate(page, btn) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    document.getElementById(`page-${page}`).classList.add('active');
    btn.classList.add('active');
    document.getElementById('page-title').textContent = titles[page] || page;
    currentPage = page;
    if (page === 'players') loadPlayers();
    if (page === 'sessions') loadSessions();
    if (page === 'logs') { queryLogs(true); loadLogPlayers(); }
  }

  // Stats
  let _lastLogTotal = 0;
  async function loadStats() {
    try {
      const d = await apiFetch('/admin/api/stats');
      document.getElementById('stat-players').textContent  = d.total_players;
      document.getElementById('stat-sessions').textContent = d.active_sessions;
      document.getElementById('stat-logs').textContent     = (d.total_log_entries || 0).toLocaleString();
      document.getElementById('stat-errors').textContent   = (d.error_count || 0).toLocaleString();
      document.getElementById('stat-games').textContent    = d.total_games_played;
      document.getElementById('topbar-meta').textContent   = new Date().toLocaleTimeString();
      // Badge for new log entries on logs tab
      if (_lastLogTotal > 0 && d.total_log_entries > _lastLogTotal && currentPage !== 'logs') {
        const btn = document.getElementById('new-entries-btn');
        if (btn) { btn.style.display = 'inline-flex'; btn.textContent = `↻ ${d.total_log_entries - _lastLogTotal} new`; }
      }
      _lastLogTotal = d.total_log_entries || 0;
    } catch(e) {}
  }

  // ── Logs (server-side filtering + pagination) ──
  let _logRows = [], _logTotal = 0, _oldestId = null, _logLoading = false;

  async function loadLogPlayers() {
    try {
      const ids = await apiFetch('/admin/api/logs/players');
      const sel = document.getElementById('f-steam');
      const cur = sel.value;
      sel.innerHTML = '<option value="">All players</option>';
      ids.forEach(id => {
        const o = document.createElement('option');
        o.value = id; o.textContent = id;
        if (id === cur) o.selected = true;
        sel.appendChild(o);
      });
    } catch(e) {}
  }

  async function queryLogs(reset = true) {
    if (_logLoading) return;
    _logLoading = true;
    if (reset) { _logRows = []; _oldestId = null; }

    const params = new URLSearchParams();
    const level   = document.getElementById('f-level').value;
    const steam   = document.getElementById('f-steam').value;
    const session = document.getElementById('f-session').value.trim();
    const search  = document.getElementById('f-search').value.trim();
    if (level)   params.set('level',      level);
    if (steam)   params.set('steam_id',   steam);
    if (session) params.set('session_id', session);
    if (search)  params.set('search',     search);
    if (_oldestId && !reset) params.set('after_id', _oldestId);
    params.set('limit', '100');

    try {
      const d = await apiFetch(`/admin/api/logs?${params}`);
      if (reset) _logRows = d.rows;
      else _logRows = [..._logRows, ...d.rows];
      _logTotal = d.total;
      if (d.rows.length) _oldestId = d.rows[d.rows.length - 1].id;
      renderLogTable();
      const btn = document.getElementById('new-entries-btn');
      if (btn) btn.style.display = 'none';
    } catch(e) { toast('Failed to load logs', 'error'); }
    finally { _logLoading = false; }
  }

  function loadMore() { queryLogs(false); }

  function clearFilters() {
    document.getElementById('f-level').value   = '';
    document.getElementById('f-steam').value   = '';
    document.getElementById('f-session').value = '';
    document.getElementById('f-search').value  = '';
    queryLogs(true);
  }

  const _LEVEL_CSS = {
    error:     'badge-error',
    exception: 'badge-error',
    warning:   'badge-warning',
    info:      'badge-info',
  };

  function renderLogTable() {
    const tbody = document.getElementById('log-tbody');
    const countEl = document.getElementById('log-count');
    const loadBtn = document.getElementById('load-more-btn');

    countEl.textContent = `${_logTotal.toLocaleString()} total · showing ${_logRows.length}`;
    loadBtn.style.display = (_logRows.length < _logTotal) ? 'inline-flex' : 'none';

    if (!_logRows.length) {
      tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state"><div class="icon">📭</div>No entries match your filters</div></td></tr>';
      return;
    }

    tbody.innerHTML = _logRows.map((r, i) => {
      const lvlCls = _LEVEL_CSS[r.level] || 'badge-info';
      const ts = r.ts ? new Date(r.ts).toLocaleString() : new Date(r.received_at * 1000).toLocaleString();
      const sid = r.steam_id ? `<span class="mono" style="font-size:11px">${esc(r.steam_id)}</span>` : '<span style="color:var(--text-muted)">—</span>';
      const msgPreview = esc((r.message || '').substring(0, 120));
      return `<tr style="cursor:pointer" onclick="showDetail(${i})">
        <td class="mono" style="font-size:11px;white-space:nowrap">${esc(ts)}</td>
        <td><span class="badge ${lvlCls}">${esc(r.level)}</span></td>
        <td>${sid}</td>
        <td class="mono" style="font-size:11px">${esc(r.session_id || '—')}</td>
        <td class="mono" style="font-size:11px;color:var(--text-muted)">${esc(r.service || '—')}</td>
        <td style="max-width:0">${msgPreview}</td>
      </tr>`;
    }).join('');
  }

  function showDetail(idx) {
    const r = _logRows[idx];
    if (!r) return;
    const panel = document.getElementById('log-detail');
    const content = document.getElementById('log-detail-content');
    panel.style.display = 'block';
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });

    // Build detail view
    const ts = r.ts ? new Date(r.ts).toLocaleString() : new Date(r.received_at * 1000).toLocaleString();
    const extra = r.extra || {};
    const stack = extra.stack || '';
    const unityMsg = extra.unity_message || '';
    const dissonanceMsg = extra.dissonance_message || '';
    const networkUrl = extra.url || '';
    const networkErr = extra.error || '';
    const reqBody = extra.requestBody || '';

    let html = `<div style="display:grid;grid-template-columns:130px 1fr;gap:6px 16px;margin-bottom:16px;">
      <span style="color:var(--text-muted)">Time</span><span>${esc(ts)}</span>
      <span style="color:var(--text-muted)">Level</span><span><span class="badge ${_LEVEL_CSS[r.level]||'badge-info'}">${esc(r.level)}</span></span>
      <span style="color:var(--text-muted)">Steam ID</span><span>${esc(r.steam_id || '—')}</span>
      <span style="color:var(--text-muted)">Session</span><span>${esc(r.session_id || '—')}</span>
      <span style="color:var(--text-muted)">Service</span><span>${esc(r.service || '—')}</span>
      <span style="color:var(--text-muted)">Game</span><span>${esc(r.game_name || '—')} ${r.game_mode ? '· '+esc(r.game_mode) : ''} ${r.game_region ? '· '+esc(r.game_region) : ''}</span>
      <span style="color:var(--text-muted)">Version</span><span class="mono" style="font-size:11px">${esc(r.version || '—')}</span>
      <span style="color:var(--text-muted)">Message</span><span style="color:var(--text)">${esc(r.message)}</span>
    </div>`;

    if (unityMsg) html += `<div style="margin-bottom:12px;"><div style="color:var(--text-muted);margin-bottom:4px;">Unity Error</div><div style="color:#fca5a5;background:#1a0808;padding:10px;border-radius:6px;">${esc(unityMsg)}</div></div>`;
    if (networkUrl) html += `<div style="margin-bottom:12px;"><div style="color:var(--text-muted);margin-bottom:4px;">Network Request</div><div style="background:var(--surface2);padding:10px;border-radius:6px;"><div style="color:#60a5fa;">${esc(networkUrl)}</div>${networkErr ? `<div style="color:#fca5a5;margin-top:4px;">Error: ${esc(networkErr)}</div>` : ''}</div></div>`;
    if (reqBody) html += `<div style="margin-bottom:12px;"><div style="color:var(--text-muted);margin-bottom:4px;">Request Body</div><pre style="background:var(--surface2);padding:10px;border-radius:6px;overflow-x:auto;margin:0;font-size:11px;">${esc(reqBody)}</pre></div>`;
    if (dissonanceMsg) html += `<div style="margin-bottom:12px;"><div style="color:var(--text-muted);margin-bottom:4px;">Dissonance</div><div style="background:var(--surface2);padding:10px;border-radius:6px;color:#a78bfa;">${esc(dissonanceMsg)}</div></div>`;
    if (stack) {
      html += `<div><div style="color:var(--text-muted);margin-bottom:4px;">Stack Trace</div>
        <pre style="background:#0a0c12;padding:12px;border-radius:6px;overflow-x:auto;margin:0;font-size:11px;line-height:1.7;color:#94a3b8;max-height:350px;overflow-y:auto;">${esc(stack.replace(/\\r\\n/g,'\n'))}</pre></div>`;
    }

    content.innerHTML = html;
  }

  async function clearLogs() {
    if (!confirm('Purge ALL log entries from the database? This cannot be undone.')) return;
    await fetch('/admin/api/logs/clear', { method: 'POST', credentials: 'same-origin' });
    _logRows = []; _logTotal = 0; _oldestId = null;
    renderLogTable();
    toast('All logs purged');
  }

  async function renderOverviewLogs() {
    try {
      const d = await apiFetch('/admin/api/logs?limit=20');
      const el = document.getElementById('overview-logs');
      if (!d.rows || !d.rows.length) { el.innerHTML = '<div style="padding:20px;color:var(--text-muted)">No logs yet.</div>'; return; }
      const _lvl = { error:'#fca5a5', exception:'#fca5a5', warning:'#fcd34d', info:'#94a3b8' };
      el.innerHTML = d.rows.map(r => {
        const ts = r.ts ? new Date(r.ts).toLocaleTimeString() : new Date(r.received_at*1000).toLocaleTimeString();
        const col = _lvl[r.level] || '#94a3b8';
        return `<div style="padding:3px 0;border-bottom:1px solid #ffffff06;display:flex;gap:8px;align-items:baseline;">
          <span style="color:#475569;font-size:11px;flex-shrink:0">${ts}</span>
          <span style="color:${col};font-size:11px;flex-shrink:0;font-weight:600">${esc(r.level.toUpperCase())}</span>
          ${r.steam_id ? `<span style="color:#7c3aed;font-size:10px;flex-shrink:0">${esc(r.steam_id.slice(-6))}</span>` : ''}
          <span style="color:#cbd5e1;font-size:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">${esc(r.message)}</span>
        </div>`;
      }).join('');
    } catch(e) {}
  }

  // Players
  let allPlayers = [];
  async function loadPlayers() {
    const tbody = document.getElementById('players-tbody');
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><div class="icon">⏳</div>Loading...</div></td></tr>';
    try {
      allPlayers = await apiFetch('/admin/api/players');
      filterPlayers();
    } catch(e) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><div class="icon">❌</div>Failed to load</div></td></tr>';
    }
  }

  function filterPlayers() {
    const search = document.getElementById('player-search').value.toLowerCase();
    const filtered = allPlayers.filter(p => !search || p.steam_id.includes(search));
    const tbody = document.getElementById('players-tbody');
    if (!filtered.length) { tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><div class="icon">👤</div>No players found</div></td></tr>'; return; }
    tbody.innerHTML = filtered.map(p => `<tr>
      <td><span class="mono">${esc(p.steam_id)}</span></td>
      <td>${p.rank}</td>
      <td>${Math.round(p.credits).toLocaleString()}</td>
      <td>${p.total_games}</td>
      <td>${p.kickstarter_backer ? '<span class="badge badge-success">YES</span>' : '<span class="badge badge-info">NO</span>'}</td>
      <td class="mono">${p.created_at ? new Date(p.created_at*1000).toLocaleDateString() : '—'}</td>
      <td class="mono">${p.updated_at ? new Date(p.updated_at*1000).toLocaleDateString() : '—'}</td>
    </tr>`).join('');
  }

  // Sessions
  async function loadSessions() {
    const tbody = document.getElementById('sessions-tbody');
    tbody.innerHTML = '<tr><td colspan="8"><div class="empty-state"><div class="icon">⏳</div>Loading...</div></td></tr>';
    try {
      const sessions = await apiFetch('/admin/api/sessions');
      if (!sessions.length) { tbody.innerHTML = '<tr><td colspan="8"><div class="empty-state"><div class="icon">🎮</div>No active sessions</div></td></tr>'; return; }
      tbody.innerHTML = sessions.map(s => `<tr>
        <td><span class="mono">${esc(s.game_id.substring(0,8))}…</span></td>
        <td>${esc(s.party_name)}</td>
        <td>${esc(s.region)}</td>
        <td><span class="badge badge-${s.status}">${s.status}</span></td>
        <td>${s.player_count} / ${s.max_players}</td>
        <td>${s.is_public ? '✅' : '🔒'}</td>
        <td><span class="mono">${esc(s.host_steam_id)}</span></td>
        <td><span class="mono">${esc(s.ip_address)}:${s.port}</span></td>
      </tr>`).join('');
    } catch(e) {
      tbody.innerHTML = '<tr><td colspan="8"><div class="empty-state"><div class="icon">❌</div>Failed to load</div></td></tr>';
    }
  }

  // Actions
  let cmdHistory = [];
  function addCmdHistory(msg) {
    const ts = new Date().toLocaleTimeString();
    cmdHistory.unshift(`[${ts}] ${msg}`);
    document.getElementById('cmd-history').innerHTML = cmdHistory.slice(0,20).join('<br>') || 'No commands sent this session.';
  }

  async function sendBroadcast() {
    const msg = document.getElementById('broadcast-msg').value.trim();
    if (!msg) { toast('Enter a message first', 'error'); return; }
    try {
      await apiFetch('/admin/api/broadcast', { method: 'POST', body: JSON.stringify({ message: msg }) });
      toast('Broadcast sent!');
      addCmdHistory(`BROADCAST: "${msg}"`);
      document.getElementById('broadcast-msg').value = '';
    } catch(e) { toast('Failed to send broadcast', 'error'); }
  }

  function confirmForceExit() {
    if (!confirm('⚠️ Force-quit all connected game clients?\n\nPlayers will lose unsaved progress. This cannot be undone.')) return;
    doForceExit();
  }

  async function doForceExit() {
    try {
      await apiFetch('/admin/api/force-exit', { method: 'POST' });
      toast('Force exit command sent!', 'success');
      addCmdHistory('FORCE EXIT sent to all clients');
    } catch(e) { toast('Failed to send force exit', 'error'); }
  }

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
  }

  // Stats poll every 5s (lightweight — just counts, no log data)
  setInterval(() => {
    loadStats();
    if (currentPage === 'sessions') loadSessions();
  }, 5000);

  // Initial load
  loadStats();
  renderOverviewLogs();
  // Refresh overview logs every 10s
  setInterval(renderOverviewLogs, 10000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Auth helpers — used by all protected routes
# ---------------------------------------------------------------------------

def _authed(session: Optional[str]) -> bool:
    if not ADMIN_PASSWORD:
        return True  # no password set → open (warn in startup log)
    return _valid_session(session)


def _redirect_to_login():
    return RedirectResponse(url="/admin/login", status_code=303)


# ---------------------------------------------------------------------------
# Routes — login / logout
# ---------------------------------------------------------------------------

@router.get("/admin/login", response_class=HTMLResponse)
async def login_page():
    return _LOGIN_HTML.replace("{err_class}", "").replace("{err_msg}", "")


@router.post("/admin/login")
async def login_submit(request: Request, password: str = Form(...)):
    ip = request.client.host

    if _rate_limited(ip):
        html = _LOGIN_HTML.replace("{err_class}", "show").replace(
            "{err_msg}", "Too many attempts. Wait a minute and try again."
        )
        return HTMLResponse(content=html, status_code=429)

    if not ADMIN_PASSWORD or hmac.compare_digest(password, ADMIN_PASSWORD):
        token = _new_session()
        resp = RedirectResponse(url="/admin", status_code=303)
        _set_session_cookie(resp, token)
        return resp

    html = _LOGIN_HTML.replace("{err_class}", "show").replace("{err_msg}", "Incorrect password.")
    return HTMLResponse(content=html, status_code=401)


@router.post("/admin/logout")
async def logout(admin_session: Optional[str] = Cookie(None)):
    _sessions.pop(admin_session, None)
    resp = JSONResponse({"status": "logged out"})
    _clear_session_cookie(resp)
    return resp


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def admin_panel(admin_session: Optional[str] = Cookie(None)):
    if not _authed(admin_session):
        return _redirect_to_login()
    return _HTML


# ---------------------------------------------------------------------------
# Admin API — all protected by session cookie
# ---------------------------------------------------------------------------

@router.get("/admin/api/stats")
async def admin_stats(admin_session: Optional[str] = Cookie(None)):
    if not _authed(admin_session):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    async with db.get_db() as conn:
        async with conn.execute("SELECT COUNT(*) as c FROM players") as cur:
            total_players = (await cur.fetchone())["c"]
        async with conn.execute("SELECT COALESCE(SUM(total_games), 0) as s FROM players") as cur:
            total_games = (await cur.fetchone())["s"]
        async with conn.execute("SELECT COUNT(*) as c FROM log_entries") as cur:
            total_logs = (await cur.fetchone())["c"]
        async with conn.execute("SELECT COUNT(*) as c FROM log_entries WHERE level='error'") as cur:
            error_count = (await cur.fetchone())["c"]

    return {
        "total_players":     total_players,
        "active_sessions":   len(ps._parties),
        "total_log_entries": total_logs,
        "error_count":       error_count,
        "total_games_played": int(total_games),
    }


@router.get("/admin/api/logs")
async def admin_logs(
    admin_session: Optional[str] = Cookie(None),
    level:      Optional[str] = None,
    steam_id:   Optional[str] = None,
    session_id: Optional[str] = None,
    search:     Optional[str] = None,
    after_id:   Optional[int] = None,
    limit: int = 100,
):
    if not _authed(admin_session):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    rows, total = await logs_db.query(
        level=level or None,
        steam_id=steam_id or None,
        session_id=session_id or None,
        search=search or None,
        after_id=after_id,
        limit=min(limit, 200),
    )
    return {"rows": rows, "total": total, "returned": len(rows)}


@router.get("/admin/api/logs/players")
async def admin_logs_players(admin_session: Optional[str] = Cookie(None)):
    if not _authed(admin_session):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await logs_db.get_known_steam_ids()


@router.post("/admin/api/logs/clear")
async def admin_logs_clear(admin_session: Optional[str] = Cookie(None)):
    if not _authed(admin_session):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    deleted = await logs_db.delete_before(time.time() + 1)
    return {"status": "cleared", "deleted": deleted}


@router.get("/admin/api/players")
async def admin_players(admin_session: Optional[str] = Cookie(None)):
    if not _authed(admin_session):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    async with db.get_db() as conn:
        async with conn.execute(
            "SELECT steam_id, rank, credits, funds, total_games, kickstarter_backer, created_at, updated_at "
            "FROM players ORDER BY updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/admin/api/sessions")
async def admin_sessions(admin_session: Optional[str] = Cookie(None)):
    if not _authed(admin_session):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    result = []
    for game_id, party in ps._parties.items():
        result.append({
            "game_id": game_id,
            "party_name": party.get("partyName", ""),
            "region": party.get("region", ""),
            "status": party.get("status", "unknown"),
            "player_count": len(party.get("players", {})),
            "max_players": party.get("maxPlayers", 0),
            "is_public": party.get("isPublic", False),
            "host_steam_id": party.get("hostSteamId", ""),
            "ip_address": party.get("ipAddress", ""),
            "port": party.get("port", 0),
            "last_heartbeat": party.get("lastHeartbeat", 0),
        })
    return result


class BroadcastRequest(BaseModel):
    message: str


@router.post("/admin/api/broadcast")
async def admin_broadcast(body: BroadcastRequest, admin_session: Optional[str] = Cookie(None)):
    if not _authed(admin_session):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    PENDING_COMMANDS["broadcast"]["version"] += 1
    PENDING_COMMANDS["broadcast"]["message"] = body.message
    await logs_db.ingest({"level": "info", "service": "admin", "message": f"ADMIN BROADCAST: {body.message}"})
    return {"status": "ok", "version": PENDING_COMMANDS["broadcast"]["version"]}


@router.post("/admin/api/force-exit")
async def admin_force_exit(admin_session: Optional[str] = Cookie(None)):
    if not _authed(admin_session):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    PENDING_COMMANDS["force_exit"]["version"] += 1
    msg = f"ADMIN FORCE EXIT issued (version {PENDING_COMMANDS['force_exit']['version']})"
    await logs_db.ingest({"level": "warning", "service": "admin", "message": msg})
    return {"status": "ok", "version": PENDING_COMMANDS["force_exit"]["version"]}
