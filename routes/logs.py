from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from log_server import LOG_BUFFER

router = APIRouter()

@router.get("/logs", response_class=HTMLResponse)
async def get_logs_page():
    # Simple HTML with polling
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Windwalk Game Logs</title>
        <style>
            :root { --bg: #1e1e1e; --text: #d4d4d4; --acc: #3794ff; --border: #333; }
            body { 
                font-family: 'Consolas', 'Monaco', monospace; 
                background: var(--bg); color: var(--text); 
                margin: 0; display: flex; flex-direction: column; height: 100vh; overflow: hidden;
            }
            header { 
                padding: 10px 20px; background: #252526; border-bottom: 1px solid var(--border);
                display: flex; gap: 15px; align-items: center; justify-content: space-between;
                flex-shrink: 0;
            }
            h1 { margin: 0; font-size: 16px; color: var(--acc); }
            
            .controls { display: flex; gap: 10px; align-items: center; }
            
            /* Tabs */
            .tabs { display: flex; gap: 2px; background: #333; padding: 2px; border-radius: 4px; }
            .tab { 
                background: transparent; border: none; color: #888; padding: 4px 12px; cursor: pointer; border-radius: 2px; font-size: 13px;
            }
            .tab:hover { color: white; background: #444; }
            .tab.active { background: #3794ff; color: white; font-weight: bold; }

            input[type="text"] { 
                background: #3c3c3c; border: 1px solid var(--border); color: white; padding: 4px 8px; border-radius: 4px; outline: none;
            }
            button.action-btn {
                background: #3c3c3c; border: 1px solid var(--border); color: white; padding: 4px 10px; border-radius: 4px; cursor: pointer;
            }
            button.action-btn:hover { background: #4c4c4c; }
            
            #log-container { 
                flex: 1; overflow-y: auto; padding: 10px; scroll-behavior: auto;
            }
            
            .entry { padding: 4px 0; border-bottom: 1px solid #2a2a2a; line-height: 1.4; font-size: 14px; }
            .entry:hover { background: #2a2d2e; }
            .timestamp { color: #858585; margin-right: 10px; user-select: none; }
            
            .tag { display: inline-block; padding: 0 4px; border-radius: 3px; margin-right: 6px; font-size: 12px; font-weight: bold; min-width: 40px; text-align: center; }
            .tag.tcp { background: #a64dff; color: white; }
            .tag.api { background: #ff9900; color: black; }
            .tag.lobby { background: #3794ff; color: white; }
            .tag.server { background: #4caf50; color: white; }
            .tag.info { background: #555; color: #ccc; }
            .tag.warn { background: #cca700; color: black; }
            .tag.error { background: #f44336; color: white; }
            .tag.action { background: #00bcd4; color: white; }
            .tag.success { background: #4caf50; color: white; }

            .msg-content { color: #d4d4d4; }
        </style>
    </head>
    <body>
        <header>
            <h1>Parasite Log Server</h1>
            
            <div class="tabs">
                <button class="tab active" onclick="setTab('ALL')">ALL</button>
                <button class="tab" onclick="setTab('LOBBY')">LOBBY</button>
                <button class="tab" onclick="setTab('TCP')">TCP</button>
                <button class="tab" onclick="setTab('API')">API</button>
            </div>

            <div class="controls">
                <input type="text" id="filter" placeholder="Text filter..." onkeyup="renderLogs()">
                <label><input type="checkbox" id="autoscroll" checked> Auto-scroll</label>
                <button class="action-btn" onclick="clearLogs()">Clear</button>
            </div>
        </header>
        <div id="log-container"></div>

        <script>
            let allLogs = [];
            let currentTab = 'ALL';
            let stickToBottom = true;
            const container = document.getElementById('log-container');

            function setTab(tab) {
                currentTab = tab;
                document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
                event.target.classList.add('active');
                renderLogs();
            }

            container.addEventListener('scroll', () => {
                stickToBottom = (container.scrollHeight - container.scrollTop - container.clientHeight < 20);
                document.getElementById('autoscroll').checked = stickToBottom;
            });

            document.getElementById('autoscroll').addEventListener('change', (e) => {
                stickToBottom = e.target.checked;
                if(stickToBottom) scrollToBottom();
            });

            async function fetchLogs() {
                try {
                    const response = await fetch('/logs/json');
                    const newLogs = await response.json();
                    if (newLogs.length !== allLogs.length) {
                        allLogs = newLogs;
                        renderLogs();
                    }
                } catch (e) { console.error(e); }
            }

            function renderLogs() {
                const textFilter = document.getElementById('filter').value.toLowerCase();
                
                const filtered = allLogs.filter(line => {
                    const lower = line.toLowerCase();
                    if (!lower.includes(textFilter)) return false;

                    if (currentTab === 'ALL') return true;
                    
                    // Category Matching based on prefix
                    if (currentTab === 'TCP' && lower.includes('[tcp]')) return true;
                    if (currentTab === 'API' && lower.includes('[api]')) return true;
                    if (currentTab === 'LOBBY') {
                        // Lobby catches everything else typically, or specific Lobby/Info tags
                        if (lower.includes('[lobby]') || lower.includes('[info]') || lower.includes('[warning]') || lower.includes('[error]') || lower.includes('[action]') || lower.includes('[success]')) {
                            // Exclude TCP/API if they happen to share tags (unlikely with current schema)
                            if (!lower.includes('[tcp]') && !lower.includes('[api]')) return true;
                        }
                    }
                    return false;
                });

                container.innerHTML = filtered.map(formatLine).join('');
                if (stickToBottom) scrollToBottom();
            }

            function formatLine(line) {
                // Parse Timestamp: [HH:MM:SS] ...
                let timestamp = "";
                let content = line;
                const match = line.match(/^(\[\d{2}:\d{2}:\d{2}\])\s*(.*)/);
                if (match) {
                    timestamp = match[1];
                    content = match[2];
                }

                // Detect Tag
                let tagHtml = "";
                
                if (content.includes('[TCP]')) tagHtml = '<span class="tag tcp">TCP</span>';
                else if (content.includes('[API]')) tagHtml = '<span class="tag api">API</span>';
                else if (content.includes('[Lobby]')) tagHtml = '<span class="tag lobby">LOBBY</span>';
                else if (content.includes('[Error]')) tagHtml = '<span class="tag error">ERROR</span>';
                else if (content.includes('[Warning]')) tagHtml = '<span class="tag warn">WARN</span>';
                else if (content.includes('[Action]')) tagHtml = '<span class="tag action">ACTION</span>';
                else if (content.includes('[Success]')) tagHtml = '<span class="tag success">SUCCESS</span>';
                else tagHtml = '<span class="tag info">INFO</span>';

                // Clean content of the tag text if redundant? 
                // Actually, the Mod sends "[TCP] msg". So we can hide the "[TCP]" text if we use a badge.
                // For now, let's just display the full line but with a nice badge.
                
                return `<div class="entry">
                    <span class="timestamp">${timestamp}</span>
                    ${tagHtml}
                    <span class="msg-content">${escapeHtml(content)}</span>
                </div>`;
            }

            function scrollToBottom() { container.scrollTop = container.scrollHeight; }
            function escapeHtml(text) {
                const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
                return text.replace(/[&<>"']/g, function(m) { return map[m]; });
            }
            function clearLogs() {
                fetch('/logs/clear', { method: 'POST' });
                allLogs = [];
                renderLogs();
            }

            setInterval(fetchLogs, 1000);
            window.onload = fetchLogs;
        </script>
    </body>
    </html>
    """
    return html_content

@router.post("/logs/clear")
async def clear_logs():
    LOG_BUFFER.clear()
    return {"status": "cleared"}

@router.get("/logs/json")
async def get_logs_json():
    return list(LOG_BUFFER)
