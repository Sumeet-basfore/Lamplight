"""
Story Studio, Worldbuilding & Persistent Mini-App Workshop Engine for Lamplight.
Provides:
1. Structured Story Bible (Characters, Factions, Locations, World Rules, Timelines, Canon Status).
2. Scene Board with Act/Chapter organization and version history comparison.
3. Continuity & Contradiction Checker (flags lore breaks, timeline glitches, and character discrepancies).
4. Interactive Worldbuilder (SVG/Canvas nodes, settlements, routes, and timeline era scrubbing).
5. Persistent Sandboxed Mini-App Workshop with isolated local storage and HTML exports.
"""

import json
import os
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
import requests

DB_PATH = os.path.expanduser("~/ChatBots/chatbots.db")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# Built-in Default Mini-Apps
BUILTIN_MINI_APPS = [
    {
        "id": "app_word_pacing",
        "title": "Writing Pacing & Readability Counter",
        "description": "Live word count, target pacing, dialogue-to-narrative ratio, and reading time.",
        "category": "writing",
        "icon": "📊",
        "is_pinned": 1,
        "html_code": """
<div class="app-wrap">
  <h2>Writing Pacing & Readability Counter</h2>
  <div class="stats-grid">
    <div class="stat-card"><span class="val" id="cntWords">0</span><span class="lbl">Words</span></div>
    <div class="stat-card"><span class="val" id="cntChars">0</span><span class="lbl">Characters</span></div>
    <div class="stat-card"><span class="val" id="cntReadTime">0m</span><span class="lbl">Reading Time</span></div>
    <div class="stat-card"><span class="val" id="cntDialogue">0%</span><span class="lbl">Dialogue Ratio</span></div>
  </div>
  <textarea id="pacingText" placeholder="Paste or draft your scene prose here to analyze pacing and statistics in real-time..."></textarea>
</div>
""",
        "css_code": """
.app-wrap { font-family: system-ui, sans-serif; color: #f1f5f9; padding: 12px; }
h2 { margin: 0 0 12px; font-size: 1.1rem; color: #f43f5e; }
.stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px; }
.stat-card { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 8px; text-align: center; }
.stat-card .val { font-size: 1.3rem; font-weight: 700; color: #38bdf8; display: block; }
.stat-card .lbl { font-size: 0.65rem; text-transform: uppercase; color: #94a3b8; }
textarea { width: 100%; height: 280px; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; color: #f8fafc; padding: 12px; font-size: 0.9rem; line-height: 1.6; resize: vertical; box-sizing: border-box; }
textarea:focus { outline: none; border-color: #f43f5e; }
""",
        "js_code": """
const txt = document.getElementById('pacingText');
txt.addEventListener('input', () => {
  const v = txt.value;
  const words = v.trim() ? v.trim().split(/\\s+/).length : 0;
  const chars = v.length;
  const readMin = Math.ceil(words / 225);
  const quoteMatches = v.match(/["“][^"”]*["”]/g) || [];
  const dialogueWords = quoteMatches.reduce((acc, q) => acc + q.trim().split(/\\s+/).length, 0);
  const diagRatio = words > 0 ? Math.round((dialogueWords / words) * 100) : 0;

  document.getElementById('cntWords').textContent = words;
  document.getElementById('cntChars').textContent = chars;
  document.getElementById('cntReadTime').textContent = readMin + 'm';
  document.getElementById('cntDialogue').textContent = diagRatio + '%';
});
"""
    },
    {
        "id": "app_sprint_timer",
        "title": "Word Sprint & Focus Timer",
        "description": "Timed burst writing sessions with live words-per-minute speedometer.",
        "category": "productivity",
        "icon": "⏱️",
        "is_pinned": 1,
        "html_code": """
<div class="sprint-wrap">
  <h2>Sprint & Velocity Tracker</h2>
  <div class="timer-box">
    <div id="timeDisplay">15:00</div>
    <div class="controls">
      <button id="btnStartSprint">Start Sprint</button>
      <button id="btnResetSprint">Reset</button>
    </div>
  </div>
  <div class="sprint-stats">
    <span>Words Added: <strong id="sprintWordCount">0</strong></span>
    <span>Speed: <strong id="sprintWpm">0 WPM</strong></span>
  </div>
  <textarea id="sprintEditor" placeholder="When the sprint begins, write without pausing or self-editing..."></textarea>
</div>
""",
        "css_code": """
.sprint-wrap { font-family: system-ui, sans-serif; color: #f1f5f9; padding: 12px; }
h2 { margin: 0 0 10px; font-size: 1.1rem; color: #facc15; }
.timer-box { display: flex; align-items: center; justify-content: space-between; background: rgba(0,0,0,0.3); border: 1px solid rgba(250,204,21,0.25); border-radius: 8px; padding: 10px 16px; margin-bottom: 12px; }
#timeDisplay { font-size: 2rem; font-weight: 700; color: #facc15; font-family: monospace; }
.controls button { background: #facc15; color: #000; border: none; padding: 8px 14px; font-weight: 600; border-radius: 6px; cursor: pointer; margin-left: 6px; }
.sprint-stats { display: flex; gap: 20px; font-size: 0.85rem; margin-bottom: 8px; color: #94a3b8; }
.sprint-stats strong { color: #38bdf8; }
textarea { width: 100%; height: 260px; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.15); border-radius: 8px; color: #f8fafc; padding: 12px; font-size: 0.9rem; line-height: 1.6; resize: vertical; box-sizing: border-box; }
""",
        "js_code": """
let secLeft = 900;
let timer = null;
let startWords = 0;
const ed = document.getElementById('sprintEditor');
const tDisp = document.getElementById('timeDisplay');
const btnStart = document.getElementById('btnStartSprint');

function updateDisplay() {
  const m = Math.floor(secLeft / 60);
  const s = secLeft % 60;
  tDisp.textContent = `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

btnStart.addEventListener('click', () => {
  if (timer) {
    clearInterval(timer);
    timer = null;
    btnStart.textContent = 'Resume Sprint';
  } else {
    startWords = ed.value.trim() ? ed.value.trim().split(/\\s+/).length : 0;
    btnStart.textContent = 'Pause Sprint';
    timer = setInterval(() => {
      if (secLeft > 0) {
        secLeft--;
        updateDisplay();
        const curWords = ed.value.trim() ? ed.value.trim().split(/\\s+/).length : 0;
        const diff = Math.max(0, curWords - startWords);
        document.getElementById('sprintWordCount').textContent = diff;
        const elapsedMins = (900 - secLeft) / 60;
        const wpm = elapsedMins > 0 ? Math.round(diff / elapsedMins) : 0;
        document.getElementById('sprintWpm').textContent = wpm + ' WPM';
      } else {
        clearInterval(timer);
        timer = null;
        alert('Sprint complete! Great work.');
      }
    }, 1000);
  }
});

document.getElementById('btnResetSprint').addEventListener('click', () => {
  if (timer) clearInterval(timer);
  timer = null;
  secLeft = 900;
  btnStart.textContent = 'Start Sprint';
  updateDisplay();
});
"""
    }
]


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_story_studio_tables():
    """Initializes tables for structured Story Bible, Scenes, Maps, and Mini-Apps."""
    with _get_db() as conn:
        c = conn.cursor()

        # 1. Story Entities (Characters, Factions, Locations, Rules, Timelines)
        c.execute("""
            CREATE TABLE IF NOT EXISTS story_entities (
                id TEXT PRIMARY KEY,
                workspace_id TEXT DEFAULT 'ws_default',
                entity_type TEXT NOT NULL, -- character, location, faction, world_rule, event, item
                name TEXT NOT NULL,
                summary TEXT DEFAULT '',
                details_json TEXT DEFAULT '{}',
                canon_status TEXT DEFAULT 'canon', -- canon, draft, hypothesis, discarded
                tags TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_story_ent_ws ON story_entities(workspace_id, entity_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_story_ent_canon ON story_entities(canon_status)")

        # 2. Story Scenes & Acts
        c.execute("""
            CREATE TABLE IF NOT EXISTS story_scenes (
                id TEXT PRIMARY KEY,
                workspace_id TEXT DEFAULT 'ws_default',
                act_or_chapter TEXT DEFAULT 'Act 1',
                scene_number INTEGER DEFAULT 1,
                title TEXT NOT NULL,
                summary TEXT DEFAULT '',
                pov_character_id TEXT,
                location_id TEXT,
                content TEXT DEFAULT '',
                status TEXT DEFAULT 'outline', -- outline, draft, revised, completed
                conflict TEXT DEFAULT '',
                word_count INTEGER DEFAULT 0,
                order_index INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_story_scenes_ws ON story_scenes(workspace_id, act_or_chapter)")

        # 3. Scene Version History
        c.execute("""
            CREATE TABLE IF NOT EXISTS story_scene_versions (
                id TEXT PRIMARY KEY,
                scene_id TEXT NOT NULL,
                version_num INTEGER NOT NULL,
                content TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(scene_id) REFERENCES story_scenes(id) ON DELETE CASCADE
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_scene_versions ON story_scene_versions(scene_id, version_num DESC)")

        # 4. Interactive World Maps
        c.execute("""
            CREATE TABLE IF NOT EXISTS story_maps (
                id TEXT PRIMARY KEY,
                workspace_id TEXT DEFAULT 'ws_default',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                width INTEGER DEFAULT 1000,
                height INTEGER DEFAULT 700,
                eras_json TEXT DEFAULT '["Ancient Era", "Age of Strife", "Imperial Age", "Modern Era"]',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 5. Map Nodes (Settlements, Citadels, Landmarks)
        c.execute("""
            CREATE TABLE IF NOT EXISTS story_map_nodes (
                id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                name TEXT NOT NULL,
                node_type TEXT DEFAULT 'settlement', -- settlement, citadel, ruins, landmark, port, pass
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                era_start INTEGER DEFAULT 0,
                era_end INTEGER DEFAULT 3,
                faction_id TEXT,
                resources TEXT DEFAULT '',
                details_json TEXT DEFAULT '{}',
                FOREIGN KEY(map_id) REFERENCES story_maps(id) ON DELETE CASCADE
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_map_nodes ON story_map_nodes(map_id)")

        # 6. Map Routes & Trade Lines
        c.execute("""
            CREATE TABLE IF NOT EXISTS story_map_routes (
                id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL,
                from_node_id TEXT NOT NULL,
                to_node_id TEXT NOT NULL,
                route_type TEXT DEFAULT 'road', -- road, river, sea, mountain_pass
                distance_leagues REAL DEFAULT 10.0,
                danger_level TEXT DEFAULT 'moderate',
                FOREIGN KEY(map_id) REFERENCES story_maps(id) ON DELETE CASCADE
            )
        """)

        # 7. Persistent Mini-App Workshop
        c.execute("""
            CREATE TABLE IF NOT EXISTS mini_apps (
                id TEXT PRIMARY KEY,
                workspace_id TEXT DEFAULT 'ws_default',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT 'writing',
                html_code TEXT NOT NULL,
                css_code TEXT DEFAULT '',
                js_code TEXT DEFAULT '',
                storage_data_json TEXT DEFAULT '{}',
                icon TEXT DEFAULT '⚡',
                is_pinned INTEGER DEFAULT 0,
                version INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Seed built-in mini apps if table is empty
        app_count = c.execute("SELECT COUNT(*) as n FROM mini_apps").fetchone()["n"]
        if app_count == 0:
            for b in BUILTIN_MINI_APPS:
                c.execute("""
                    INSERT OR REPLACE INTO mini_apps
                    (id, workspace_id, title, description, category, html_code, css_code, js_code, icon, is_pinned)
                    VALUES (?, 'ws_default', ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    b["id"], b["title"], b["description"], b["category"],
                    b["html_code"], b["css_code"], b["js_code"], b["icon"], b["is_pinned"]
                ))

        conn.commit()


# Call init on import
init_story_studio_tables()


# ============================================================================
# 1. Story Entities & Canon Bible CRUD
# ============================================================================

def create_story_entity(
    workspace_id: str = "ws_default",
    entity_type: str = "character",
    name: str = "",
    summary: str = "",
    details: Optional[Any] = None,
    canon_status: str = "canon",
    tags: Any = ""
) -> Dict[str, Any]:
    """Creates a character, location, faction, world rule, or lore event."""
    ent_id = f"ent_{uuid.uuid4().hex[:10]}"
    if isinstance(tags, list):
        tags = ", ".join(tags)
    if isinstance(details, str):
        details_str = json.dumps({"notes": details})
    else:
        details_str = json.dumps(details or {})
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO story_entities
            (id, workspace_id, entity_type, name, summary, details_json, canon_status, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ent_id, workspace_id or "ws_default", entity_type, name, summary, details_str, canon_status, str(tags)))
        conn.commit()
    return get_story_entity(ent_id)


def list_story_entities(
    workspace_id: str = "ws_default",
    entity_type: Optional[str] = None,
    canon_status: Optional[str] = None,
    query: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Lists story entities filtered by type, canon status, or text query."""
    ws = workspace_id or "ws_default"
    sql = "SELECT * FROM story_entities WHERE workspace_id = ?"
    params: List[Any] = [ws]

    if entity_type:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    if canon_status:
        sql += " AND canon_status = ?"
        params.append(canon_status)
    if query:
        sql += " AND (name LIKE ? OR summary LIKE ? OR tags LIKE ?)"
        q_wild = f"%{query}%"
        params.extend([q_wild, q_wild, q_wild])

    sql += " ORDER BY entity_type ASC, name ASC"
    with _get_db() as conn:
        c = conn.cursor()
        rows = c.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["details"] = json.loads(d["details_json"])
            except Exception:
                d["details"] = {}
            result.append(d)
        return result


def get_story_entity(entity_id: str) -> Optional[Dict[str, Any]]:
    """Fetches a single story entity."""
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM story_entities WHERE id = ?", (entity_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["details"] = json.loads(d["details_json"])
        except Exception:
            d["details"] = {}
        return d


def update_story_entity(
    entity_id: str,
    name: Optional[str] = None,
    summary: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    canon_status: Optional[str] = None,
    tags: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Updates fields of a story entity."""
    ent = get_story_entity(entity_id)
    if not ent:
        return None

    new_name = name if name is not None else ent["name"]
    new_summary = summary if summary is not None else ent["summary"]
    new_details = json.dumps(details) if details is not None else ent["details_json"]
    new_canon = canon_status if canon_status is not None else ent["canon_status"]
    new_tags = tags if tags is not None else ent["tags"]

    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE story_entities
            SET name = ?, summary = ?, details_json = ?, canon_status = ?, tags = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_name, new_summary, new_details, new_canon, new_tags, entity_id))
        conn.commit()
    return get_story_entity(entity_id)


def delete_story_entity(entity_id: str) -> bool:
    """Deletes a story entity."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM story_entities WHERE id = ?", (entity_id,))
        conn.commit()
        return c.rowcount > 0


def get_relationship_graph(workspace_id: str = "ws_default") -> Dict[str, Any]:
    """Generates a node/edge relationship graph of characters, factions, and places."""
    entities = list_story_entities(workspace_id)
    nodes = []
    edges = []

    for ent in entities:
        nodes.append({
            "id": ent["id"],
            "name": ent["name"],
            "label": ent["name"],
            "type": ent["entity_type"],
            "canon": ent["canon_status"],
            "summary": ent["summary"]
        })

        # Parse character or faction relationships
        rels = ent.get("details", {}).get("relationships", [])
        if isinstance(rels, list):
            for rel in rels:
                if isinstance(rel, dict) and "target_id" in rel:
                    edges.append({
                        "from": ent["id"],
                        "to": rel["target_id"],
                        "source": ent["id"],
                        "target": rel["target_id"],
                        "label": rel.get("type", "relates to"),
                        "notes": rel.get("notes", "")
                    })

    # Connect entities that share tags
    for i, ent1 in enumerate(entities):
        tags1 = set([t.strip().lower() for t in str(ent1.get("tags", "")).split(",") if t.strip()])
        for ent2 in entities[i+1:]:
            tags2 = set([t.strip().lower() for t in str(ent2.get("tags", "")).split(",") if t.strip()])
            shared = tags1.intersection(tags2)
            if shared:
                edges.append({
                    "from": ent1["id"],
                    "to": ent2["id"],
                    "source": ent1["id"],
                    "target": ent2["id"],
                    "label": f"Shared: {', '.join(list(shared)[:2])}"
                })

    return {"nodes": nodes, "edges": edges}


# ============================================================================
# 2. Scene Board & Version Comparison
# ============================================================================

def create_story_scene(
    workspace_id: str = "ws_default",
    act_or_chapter: str = "Act 1",
    title: str = "Untitled Scene",
    summary: str = "",
    pov_character_id: Optional[str] = None,
    location_id: Optional[str] = None,
    content: str = "",
    status: str = "draft",
    conflict: str = "",
    order_index: int = 0
) -> Dict[str, Any]:
    """Creates a new scene in the Scene Board."""
    scene_id = f"scn_{uuid.uuid4().hex[:10]}"
    w_count = len(content.strip().split()) if content.strip() else 0

    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO story_scenes
            (id, workspace_id, act_or_chapter, scene_number, title, summary,
             pov_character_id, location_id, content, status, conflict, word_count, order_index)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            scene_id, workspace_id or "ws_default", act_or_chapter or "Act 1",
            order_index + 1, title, summary, pov_character_id, location_id,
            content, status, conflict, w_count, order_index
        ))
        # Initial version
        if content:
            c.execute("""
                INSERT INTO story_scene_versions (id, scene_id, version_num, content, note)
                VALUES (?, ?, 1, ?, 'Initial creation')
            """, (f"ver_{uuid.uuid4().hex[:8]}", scene_id, content))
        conn.commit()

    return get_story_scene(scene_id)


def list_story_scenes(workspace_id: str = "ws_default", act_or_chapter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns all scenes in order."""
    ws = workspace_id or "ws_default"
    sql = "SELECT * FROM story_scenes WHERE workspace_id = ?"
    params: List[Any] = [ws]
    if act_or_chapter:
        sql += " AND act_or_chapter = ?"
        params.append(act_or_chapter)
    sql += " ORDER BY act_or_chapter ASC, order_index ASC, scene_number ASC"

    with _get_db() as conn:
        c = conn.cursor()
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_story_scene(scene_id: str) -> Optional[Dict[str, Any]]:
    """Fetches single scene with metadata."""
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM story_scenes WHERE id = ?", (scene_id,)).fetchone()
        return dict(row) if row else None


def update_story_scene(
    scene_id: str,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    content: Optional[str] = None,
    status: Optional[str] = None,
    conflict: Optional[str] = None,
    act_or_chapter: Optional[str] = None,
    order_index: Optional[int] = None,
    version_note: str = ""
) -> Optional[Dict[str, Any]]:
    """Updates scene and archives prior draft into version history if content changed."""
    scene = get_story_scene(scene_id)
    if not scene:
        return None

    new_title = title if title is not None else scene["title"]
    new_summary = summary if summary is not None else scene["summary"]
    new_content = content if content is not None else scene["content"]
    new_status = status if status is not None else scene["status"]
    new_conflict = conflict if conflict is not None else scene["conflict"]
    new_act = act_or_chapter if act_or_chapter is not None else scene["act_or_chapter"]
    new_order = order_index if order_index is not None else scene["order_index"]
    new_words = len(new_content.strip().split()) if new_content.strip() else 0

    with _get_db() as conn:
        c = conn.cursor()
        # Save version snapshot if content changed
        if content is not None and content.strip() != scene["content"].strip():
            last_ver = c.execute("SELECT MAX(version_num) as v FROM story_scene_versions WHERE scene_id = ?", (scene_id,)).fetchone()["v"] or 0
            c.execute("""
                INSERT INTO story_scene_versions (id, scene_id, version_num, content, note)
                VALUES (?, ?, ?, ?, ?)
            """, (f"ver_{uuid.uuid4().hex[:8]}", scene_id, last_ver + 1, new_content, version_note or f"Draft update v{last_ver + 1}"))

        c.execute("""
            UPDATE story_scenes
            SET title = ?, summary = ?, content = ?, status = ?, conflict = ?,
                act_or_chapter = ?, order_index = ?, word_count = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_title, new_summary, new_content, new_status, new_conflict, new_act, new_order, new_words, scene_id))
        conn.commit()

    return get_story_scene(scene_id)


def list_scene_versions(scene_id: str) -> List[Dict[str, Any]]:
    """Returns version history of a scene for rollback and diffing."""
    with _get_db() as conn:
        c = conn.cursor()
        rows = c.execute("SELECT * FROM story_scene_versions WHERE scene_id = ? ORDER BY version_num DESC", (scene_id,)).fetchall()
        return [dict(r) for r in rows]


def delete_story_scene(scene_id: str) -> bool:
    """Deletes a scene."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM story_scenes WHERE id = ?", (scene_id,))
        conn.commit()
        return c.rowcount > 0


# ============================================================================
# 3. Continuity & Lore Contradiction Checker
# ============================================================================

CONTINUITY_SYSTEM_PROMPT = """You are Muse's Continuity Supervisor and Canon Guardian.
Your task is to audit the provided scene text against the author's established Story Bible (Characters, Factions, Locations, World Rules, and Events).

Inspect for:
1. World Rule / Magic / Tech violations (e.g. violating known constraints or costs).
2. Character inconsistencies (acting out of character, using powers they don't possess, knowledge they cannot have yet).
3. Geographical / Travel contradictions (arriving impossibly fast, wrong climate or landmarks).
4. Status contradictions (characters that are deceased or imprisoned appearing normally).

Differentiate between:
- Blatant Continuity Errors (must be flagged).
- Deliberate Plot Ambiguity / Subtext (e.g. character lying or unreliable narrator).

Format your output strictly as Markdown:
### 📜 Continuity Audit Report
**Integrity Score**: [0-100]% ([Sound / Minor Warnings / Canon Breach])

- **Verified Canon Alignments**:
  - [bullet 1]
- **Potential Lore Inconsistencies & Breaches**:
  - [bullet 1 or 'None detected. Scene respects established world rules.']
- **Author Inquiry & Subtext Check**:
  - [bullet 1: asking if ambiguity was intentional]
"""

def check_scene_continuity(
    workspace_id: str,
    scene_text: str,
    model: str = "Qwen3.5-uncensored:latest",
    timeout: int = 60
) -> Dict[str, Any]:
    """Runs a continuity check comparing scene text against established story entities."""
    entities = list_story_entities(workspace_id, canon_status="canon")
    if not entities:
        return {
            "ok": True,
            "score": 100,
            "report": "No established canon entities registered yet in the Story Bible. Everything written is currently founding lore.",
            "conflicts": []
        }

    # Format concise canon summary
    canon_blocks = []
    for ent in entities:
        canon_blocks.append(f"[{ent['entity_type'].upper()}] {ent['name']}: {ent['summary']} (Tags: {ent['tags']})")
    canon_context = "\n".join(canon_blocks)

    messages = [
        {"role": "system", "content": CONTINUITY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"ESTABLISHED CANON BIBLE:\n{canon_context}\n\nSCENE DRAFT UNDER AUDIT:\n{scene_text}\n\nPlease audit for continuity breaches."
        }
    ]

    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "options": {"temperature": 0.2, "top_p": 0.9, "num_predict": 2048},
                "stream": False
            },
            timeout=timeout
        )
        if r.status_code == 200:
            report = r.json().get("message", {}).get("content", "").strip()
            score_match = re.search(r"\*\*Integrity Score\*\*:\s*(\d+)%", report)
            score = int(score_match.group(1)) if score_match else 90
            return {
                "ok": True,
                "score": score,
                "report": report
            }
    except Exception as e:
        return {
            "ok": False,
            "score": 0,
            "report": f"Continuity check failed: {str(e)}"
        }


# ============================================================================
# 4. Interactive Worldbuilder Map Engine
# ============================================================================

def get_or_create_world_map(workspace_id: str = "ws_default", title: str = "World Map") -> Dict[str, Any]:
    """Retrieves or creates the primary world map for a workspace."""
    ws = workspace_id or "ws_default"
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM story_maps WHERE workspace_id = ? ORDER BY created_at ASC LIMIT 1", (ws,)).fetchone()
        if row:
            d = dict(row)
            d["eras"] = json.loads(d["eras_json"]) if isinstance(d["eras_json"], str) else d["eras_json"]
            return d

        map_id = f"map_{uuid.uuid4().hex[:8]}"
        default_eras = json.dumps(["Ancient Era", "Age of Strife", "Imperial Age", "Modern Era"])
        c.execute("""
            INSERT INTO story_maps (id, workspace_id, title, description, width, height, eras_json)
            VALUES (?, ?, ?, 'Interactive visual territory and route map', 1000, 700, ?)
        """, (map_id, ws, title, default_eras))
        conn.commit()

        # Seed sample nodes if brand new map
        c.execute("""
            INSERT INTO story_map_nodes (id, map_id, name, node_type, x, y, era_start, era_end, resources, details_json)
            VALUES
            (?, ?, 'Capital Citadel', 'citadel', 500, 350, 0, 3, 'Iron, Gold', '{"defense": "high"}'),
            (?, ?, 'Sunspire Port', 'port', 750, 480, 0, 3, 'Fish, Trade Goods', '{"trade_fleet": 40}'),
            (?, ?, 'Whispering Ruins', 'ruins', 250, 200, 1, 3, 'Arcane Relics', '{"ancient_gods": true}')
        """, (f"node_{uuid.uuid4().hex[:6]}", map_id, f"node_{uuid.uuid4().hex[:6]}", map_id, f"node_{uuid.uuid4().hex[:6]}", map_id))
        conn.commit()

        return get_or_create_world_map(ws, title)


def get_map_details(map_id: str, era_index: Optional[int] = None) -> Dict[str, Any]:
    """Returns map metadata, filtered active nodes, and connected routes for visual rendering."""
    with _get_db() as conn:
        c = conn.cursor()
        map_row = c.execute("SELECT * FROM story_maps WHERE id = ?", (map_id,)).fetchone()
        if not map_row:
            return {}
        m_dict = dict(map_row)
        m_dict["eras"] = json.loads(m_dict["eras_json"]) if isinstance(m_dict["eras_json"], str) else m_dict["eras_json"]

        # Fetch nodes
        if era_index is not None:
            node_rows = c.execute("""
                SELECT * FROM story_map_nodes
                WHERE map_id = ? AND era_start <= ? AND era_end >= ?
            """, (map_id, era_index, era_index)).fetchall()
        else:
            node_rows = c.execute("SELECT * FROM story_map_nodes WHERE map_id = ?", (map_id,)).fetchall()

        nodes = []
        for n in node_rows:
            nd = dict(n)
            try:
                nd["details"] = json.loads(nd["details_json"])
            except Exception:
                nd["details"] = {}
            nodes.append(nd)

        # Fetch routes
        route_rows = c.execute("SELECT * FROM story_map_routes WHERE map_id = ?", (map_id,)).fetchall()
        routes = [dict(r) for r in route_rows]

        m_dict["nodes"] = nodes
        m_dict["routes"] = routes
        return m_dict


def add_map_node(
    map_id: str,
    name: str,
    node_type: str = "settlement",
    x: int = 500,
    y: int = 350,
    era_start: int = 0,
    era_end: int = 3,
    faction_id: Optional[str] = None,
    resources: str = "",
    details: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Adds a location node to the world map."""
    node_id = f"node_{uuid.uuid4().hex[:8]}"
    det_str = json.dumps(details or {})
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO story_map_nodes
            (id, map_id, name, node_type, x, y, era_start, era_end, faction_id, resources, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (node_id, map_id, name, node_type, x, y, era_start, era_end, faction_id, resources, det_str))
        conn.commit()
    return {"id": node_id, "name": name, "x": x, "y": y, "node_type": node_type}


def add_map_route(
    map_id: str,
    from_node_id: str,
    to_node_id: str,
    route_type: str = "road",
    distance_leagues: float = 10.0,
    danger_level: str = "moderate"
) -> Dict[str, Any]:
    """Creates a connecting route or trade line between two map nodes."""
    route_id = f"rt_{uuid.uuid4().hex[:8]}"
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO story_map_routes
            (id, map_id, from_node_id, to_node_id, route_type, distance_leagues, danger_level)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (route_id, map_id, from_node_id, to_node_id, route_type, distance_leagues, danger_level))
        conn.commit()
    return {"id": route_id, "from_node_id": from_node_id, "to_node_id": to_node_id, "route_type": route_type}


def delete_map_node(node_id: str) -> bool:
    """Removes a map node and connected routes."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM story_map_routes WHERE from_node_id = ? OR to_node_id = ?", (node_id, node_id))
        c.execute("DELETE FROM story_map_nodes WHERE id = ?", (node_id,))
        conn.commit()
        return c.rowcount > 0


# ============================================================================
# 5. Persistent Sandboxed Mini-App Workshop
# ============================================================================

def list_mini_apps(workspace_id: str = "ws_default") -> List[Dict[str, Any]]:
    """Returns all available mini-apps for the workspace."""
    ws = workspace_id or "ws_default"
    with _get_db() as conn:
        c = conn.cursor()
        rows = c.execute("""
            SELECT id, workspace_id, title, description, category, icon, is_pinned, version, created_at, updated_at
            FROM mini_apps
            WHERE workspace_id = ? OR workspace_id = 'ws_default'
            ORDER BY is_pinned DESC, updated_at DESC
        """, (ws,)).fetchall()
        return [dict(r) for r in rows]


def get_mini_app(app_id: str) -> Optional[Dict[str, Any]]:
    """Fetches full code and storage of a mini-app."""
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM mini_apps WHERE id = ?", (app_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["storage_data"] = json.loads(d["storage_data_json"])
        except Exception:
            d["storage_data"] = {}
        return d


def save_mini_app(
    workspace_id: str = "ws_default",
    title: str = "New Mini-App",
    description: str = "",
    category: str = "writing",
    html_code: str = "",
    css_code: str = "",
    js_code: str = "",
    icon: str = "⚡",
    is_pinned: int = 0,
    app_id: Optional[str] = None
) -> Dict[str, Any]:
    """Creates or updates a persistent sandboxed mini-app."""
    aid = app_id or f"app_{uuid.uuid4().hex[:10]}"
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO mini_apps
            (id, workspace_id, title, description, category, html_code, css_code, js_code, icon, is_pinned, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            aid, workspace_id or "ws_default", title, description,
            category, html_code, css_code, js_code, icon, is_pinned
        ))
        conn.commit()
    return get_mini_app(aid)


def update_mini_app_storage(app_id: str, storage_data: Dict[str, Any]) -> bool:
    """Saves isolated state data for a mini-app."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE mini_apps
            SET storage_data_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (json.dumps(storage_data), app_id))
        conn.commit()
        return c.rowcount > 0


def delete_mini_app(app_id: str) -> bool:
    """Deletes a custom mini-app."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM mini_apps WHERE id = ?", (app_id,))
        conn.commit()
        return c.rowcount > 0


def bundle_mini_app_html(app_id: str) -> str:
    """Bundles a mini-app into a single isolated HTML document for download or sandboxed iframe preview."""
    app = get_mini_app(app_id)
    if not app:
        return "<html><body>App not found</body></html>"

    storage_init = json.dumps(app.get("storage_data", {}))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{app['title']}</title>
  <style>
    body {{ margin: 0; background: #0f172a; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif; }}
    {app['css_code']}
  </style>
</head>
<body>
  {app['html_code']}
  <script>
    const INITIAL_STORAGE = {storage_init};
    {app['js_code']}
  </script>
</body>
</html>
"""
