import re
import sqlite3
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent / "chatbots.db"
MODES = ("nsfw", "friend", "architect", "muse", "reminders", "search")


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL)"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_mode ON messages(mode, id)"
        )
        try:
            c.execute("ALTER TABLE messages ADD COLUMN model TEXT")
        except sqlite3.OperationalError:
            pass  # column already there
        c.execute(
            """CREATE TABLE IF NOT EXISTS friend_profile(
                key TEXT PRIMARY KEY, value TEXT NOT NULL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS friend_facts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_mode ON sessions(mode, updated_at)"
        )
        try:
            c.execute("ALTER TABLE messages ADD COLUMN session_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE messages ADD COLUMN skill TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN context_summary TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN workspace_id TEXT DEFAULT 'ws_default'")
        except sqlite3.OperationalError:
            pass
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id, updated_at)"
        )

        # ---------------------------------------------------------
        # Project Workspaces
        # ---------------------------------------------------------
        c.execute(
            """CREATE TABLE IF NOT EXISTS workspaces(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                instructions TEXT DEFAULT '',
                icon TEXT DEFAULT '📁',
                color TEXT DEFAULT '#38bdf8',
                repo_path TEXT DEFAULT '',
                model_pref TEXT DEFAULT '',
                is_default INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_workspaces_slug ON workspaces(slug)")

        # Seed default workspace if empty
        ws_count = c.execute("SELECT COUNT(*) AS n FROM workspaces").fetchone()["n"]
        if ws_count == 0:
            c.execute(
                """INSERT INTO workspaces(id, name, slug, description, instructions, icon, color, is_default)
                   VALUES('ws_default', 'Personal Sandbox', 'default',
                          'General personal AI workspace for open conversation, learning, and experiments',
                          'Act as a proactive, capable companion across tasks. Maintain intellectual honesty, clean structure, and sharp clarity.',
                          '📁', '#38bdf8', 1)"""
            )

        # ---------------------------------------------------------
        # Unified Memory Control Center (Global & Workspace Scoped)
        # ---------------------------------------------------------
        c.execute(
            """CREATE TABLE IF NOT EXISTS memories(
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                scope TEXT NOT NULL DEFAULT 'global', -- 'global', 'workspace'
                category TEXT DEFAULT 'fact', -- 'fact', 'preference', 'instruction', 'project'
                content TEXT NOT NULL,
                source_session_id TEXT,
                source_message_id INTEGER,
                confidence REAL DEFAULT 1.0,
                is_active INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_workspace ON memories(workspace_id, is_active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, is_active)")

        # Auto-migrate legacy friend_facts into global memories if memories is empty
        mem_count = c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        if mem_count == 0:
            facts = c.execute("SELECT * FROM friend_facts").fetchall()
            for f in facts:
                mem_id = f"mem_{uuid.uuid4().hex[:10]}"
                c.execute(
                    """INSERT OR IGNORE INTO memories(id, workspace_id, scope, category, content, confidence, is_active, created_at)
                       VALUES(?, NULL, 'global', 'fact', ?, 1.0, 1, ?)""",
                    (mem_id, f["fact"], f["created_at"])
                )

        # ---------------------------------------------------------
        # Database Backup Snapshots Log
        # ---------------------------------------------------------
        c.execute(
            """CREATE TABLE IF NOT EXISTS backup_snapshots(
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                trigger_type TEXT DEFAULT 'auto',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_backups_created ON backup_snapshots(created_at DESC)")

        c.execute(
            """CREATE TABLE IF NOT EXISTS skills(
                id TEXT PRIMARY KEY,
                command TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                icon TEXT NOT NULL,
                prompt TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'all',
                is_builtin INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_skills_command ON skills(command)")

        # Dynamic Model Providers (Ollama, LM Studio, llama.cpp, vLLM)
        c.execute(
            """CREATE TABLE IF NOT EXISTS providers(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL, -- 'ollama', 'openai_compat'
                base_url TEXT NOT NULL,
                api_key TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        if c.execute("SELECT COUNT(*) AS n FROM providers").fetchone()["n"] == 0:
            c.execute(
                "INSERT INTO providers(id, name, provider_type, base_url, api_key, is_active) VALUES(?,?,?,?,?,?)",
                ("p_ollama", "Local Ollama", "ollama", "http://localhost:11434", "", 1)
            )

        # Model Lab Experiments & Runs
        c.execute(
            """CREATE TABLE IF NOT EXISTS model_experiments(
                id TEXT PRIMARY KEY,
                suite_name TEXT DEFAULT '',
                prompt TEXT NOT NULL,
                mode TEXT DEFAULT 'transparent', -- 'transparent', 'blind'
                execution_mode TEXT DEFAULT 'sequential', -- 'sequential', 'parallel'
                winner_model TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS model_experiment_runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT NOT NULL REFERENCES model_experiments(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                provider_id TEXT DEFAULT 'p_ollama',
                response_text TEXT NOT NULL,
                tokens_prompt INTEGER DEFAULT 0,
                tokens_completion INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                ttft_ms INTEGER DEFAULT 0,
                tokens_per_sec REAL DEFAULT 0.0,
                vram_delta_mb REAL DEFAULT 0.0,
                rating INTEGER DEFAULT 0,
                user_voted INTEGER DEFAULT 0)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_runs_experiment ON model_experiment_runs(experiment_id)")

        # Benchmark Prompt Suites
        c.execute(
            """CREATE TABLE IF NOT EXISTS lab_prompt_suites(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                prompt TEXT NOT NULL,
                is_builtin INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        if c.execute("SELECT COUNT(*) AS n FROM lab_prompt_suites").fetchone()["n"] == 0:
            default_suites = [
                ("suite_code_lru", "Thread-safe LRU Cache", "Coding",
                 "Write a production-grade, thread-safe LRU Cache in Python using a doubly linked list and dictionary. Include full type hints, docstrings, O(1) get and put complexity, and a thorough explanation of locking concurrency.", 1),
                ("suite_logic_hats", "Three Wise Logicians", "Reasoning",
                 "Three logicians (A, B, C) sit in a circle. The referee places a colored hat (red or blue) on each head. Exactly two wear red hats. Each can see the other two hats but not their own. When asked 'Do you know your hat color?', A says 'No', then B says 'No'. Can C determine their hat color? Explain C's exact deduction step by step.", 1),
                ("suite_creative_noir", "Cyberpunk Rain Noir", "Creative",
                 "Write a tight 3-paragraph hardboiled monologue from the perspective of an aging detective with a glitching ocular implant, tracking a fugitive bio-hacker through an alleyway in a perpetual neon rainstorm. Emphasize visceral sensory details and psychological weight.", 1),
                ("suite_arch_tradeoffs", "Monolith vs Microservices", "Architecture",
                 "Provide a structured, unbiased architectural comparison between an optimized Modular Monolith and an Event-Driven Microservices mesh. Detail specific inflection points (team size, state consistency, operational overhead, failure modes) that determine when to migrate.", 1)
            ]
            c.executemany(
                "INSERT INTO lab_prompt_suites(id, name, category, prompt, is_builtin) VALUES(?,?,?,?,?)",
                default_suites
            )

        # AI Arena Sessions (Council & Debate)
        c.execute(
            """CREATE TABLE IF NOT EXISTS arena_sessions(
                id TEXT PRIMARY KEY,
                arena_type TEXT NOT NULL, -- 'council', 'debate'
                title TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT '',
                judge_model TEXT DEFAULT '',
                models_json TEXT DEFAULT '[]',
                rounds_json TEXT DEFAULT '[]',
                verdict_text TEXT DEFAULT '',
                verdict_json TEXT DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_arena_sessions_type ON arena_sessions(arena_type, created_at DESC)")

        # Document Hub (Persistent Local Documents & Study Material)
        c.execute(
            """CREATE TABLE IF NOT EXISTS library_documents(
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                preview_url TEXT NOT NULL,
                char_count INTEGER DEFAULT 0,
                page_count INTEGER,
                extracted_text TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_library_docs_created ON library_documents(created_at DESC)")

        # Canvas Artifacts & Version Snapshots
        c.execute(
            """CREATE TABLE IF NOT EXISTS canvas_artifacts(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                lang TEXT NOT NULL,
                code TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                parent_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        # Model Observatory Inference Telemetry Logs
        c.execute(
            """CREATE TABLE IF NOT EXISTS inference_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                provider TEXT DEFAULT 'ollama',
                prompt_chars INTEGER DEFAULT 0,
                response_chars INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                ttft_ms INTEGER DEFAULT 0,
                tokens_per_sec REAL DEFAULT 0.0,
                status TEXT DEFAULT 'ok',
                error_msg TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_inference_logs_created ON inference_logs(created_at DESC)")

        # Reusable Prompt Library & Versioned Presets
        c.execute(
            """CREATE TABLE IF NOT EXISTS prompt_templates(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT DEFAULT 'General',
                system_prompt TEXT,
                user_template TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                is_builtin INTEGER DEFAULT 0,
                is_favorite INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_prompt_templates_cat ON prompt_templates(category)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prompt_templates_fav ON prompt_templates(is_favorite)")

        # Seed default prompt templates if empty
        template_count = c.execute("SELECT COUNT(*) AS n FROM prompt_templates").fetchone()["n"]
        if template_count == 0:
            default_templates = [
                ("pt_code_refactor", "Code Refactor & Clean Architecture",
                 "Refactor messy or legacy code into clean, modular, and idiomatic structure with clear rationale",
                 "Coding",
                 "You are an expert software engineer and clean code architect. Refactor the provided code following SOLID principles, high readability, and best practices. Present the improved code, followed by a bulleted explanation of what changed and why.",
                 "Please refactor the following code to improve maintainability, efficiency, and clarity:\n\n```\n{{code}}\n```\n\nSpecific goal: {{goal}}",
                 1, 1, 1),
                ("pt_code_tests", "Comprehensive Unit Test Suite",
                 "Generate thorough, robust unit tests with edge cases, mocks, and failure paths",
                 "Coding",
                 "You are a QA automation specialist and software engineer. Generate a comprehensive unit test suite covering happy paths, boundary values, error handling, and mock dependencies.",
                 "Write a complete, runnable unit test suite for the following code using {{framework}}:\n\n```\n{{code}}\n```",
                 1, 1, 1),
                ("pt_code_audit", "Security & Vulnerability Audit",
                 "Inspect code for OWASP vulnerabilities, race conditions, and injection flaws",
                 "Coding",
                 "You are a principal application security auditor. Perform a meticulous security review of the provided code. Highlight vulnerabilities with CVSS severity, explanation of attack vectors, and patched code snippets.",
                 "Perform a security and vulnerability audit on the following code:\n\n```\n{{code}}\n```",
                 1, 1, 0),
                ("pt_first_principles", "First Principles Decomposition",
                 "Break down difficult problems to fundamental truths and reason up from there",
                 "Analysis",
                 "You are an analytical philosopher and systems thinker. Deconstruct the user's problem down to its most fundamental physical, logical, and economic truths. Reason upwards from those bedrock axioms to formulate innovative solutions.",
                 "Analyze the following problem using first principles thinking:\n\nProblem: {{problem}}",
                 1, 1, 1),
                ("pt_devils_advocate", "Adversarial Stress Test",
                 "Ruthlessly attack an idea or plan to expose blind spots and hidden risks",
                 "Analysis",
                 "You are an adversarial red-teamer and strategic risk officer. Rigorously challenge the user's thesis or proposal. Identify hidden assumptions, failure modes, competitive threats, and unintended consequences.",
                 "Play devil's advocate and aggressively stress-test this proposal:\n\n{{thesis}}",
                 1, 1, 0),
                ("pt_executive_brief", "Executive 1-Page Decision Brief",
                 "Synthesize complex situations into an actionable executive brief for leadership",
                 "Writing",
                 "You are a chief of staff. Synthesize the provided information into a high-impact, 1-page executive brief structured with: 1) Executive Summary (BLUF), 2) Current Landscape & Core Problem, 3) Key Metrics & Findings, 4) Options Considered, and 5) Recommended Action with Next Steps.",
                 "Draft an executive decision brief on:\nSubject: {{subject}}\nKey Context & Data:\n{{data}}",
                 1, 1, 1),
                ("pt_clear_email", "High-Impact Concise Email",
                 "Draft a crisp, persuasive email with clear call-to-action and zero fluff",
                 "Productivity",
                 "You are a master business communicator. Write a concise, professional email that commands attention, states the bottom line immediately, and finishes with a crystal-clear call to action.",
                 "Write an email to: {{audience}}\nCore message and goal: {{core_message}}",
                 1, 1, 0),
                ("pt_meeting_action", "Meeting Notes to Action Matrix",
                 "Turn messy transcripts or rough notes into structured decisions and task owners",
                 "Productivity",
                 "You are an executive project manager. Extract clear decisions made, open questions, and an actionable task matrix with tasks, assignees, and deadlines from the meeting notes.",
                 "Convert these meeting notes into a structured decisions and action item matrix:\n\n{{notes}}",
                 1, 1, 0),
                ("pt_feynman_concept", "Feynman Technique Deep Dive",
                 "Explain complicated ideas with vivid analogies and plain language",
                 "Education",
                 "You are Richard Feynman. Teach the requested concept using vivid analogies, relatable physical metaphors, and plain language. Conclude with three thought experiments or self-test questions to verify understanding.",
                 "Explain this concept using the Feynman Technique:\n\nConcept: {{concept}}",
                 1, 1, 1),
                ("pt_system_design", "System Architecture & Scalability",
                 "Architect resilient, distributed backend services with trade-off analysis",
                 "Coding",
                 "You are a principal distributed systems architect. Design a robust, scalable architecture for the requirements. Detail the data model, API contracts, caching strategy, queueing, storage, failure recovery, and trade-offs.",
                 "Design the system architecture for:\nGoal: {{system_goal}}\nScale: {{traffic_scale}}",
                 1, 1, 0)
            ]
            c.executemany(
                """INSERT OR IGNORE INTO prompt_templates
                   (id, title, description, category, system_prompt, user_template, version, is_builtin, is_favorite)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                default_templates
            )

        # Seed default built-in skills if skills table is empty
        skill_count = c.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"]
        if skill_count == 0:
            default_skills = [
                ("sk_summarize", "summarize", "Summarize & Key Points",
                 "Condense messages or documents into clear takeaways", "📝",
                 "You are an expert summarizer. Synthesize the provided text, documents, or context into clear, concise, and structured takeaways with bullet points. Highlight main ideas, key facts, and actionable conclusions.", "all", 1),
                ("sk_study", "study", "Deep Study & Tutor",
                 "Master any concept with simple analogies and Socratic guidance", "🎓",
                 "You are an exceptional personal tutor. Break down the user's topic step-by-step using clear analogies, intuitive explanations, real-world examples, and mental models. Check for understanding and encourage curiosity.", "all", 1),
                ("sk_code", "code", "Code Architect & Debug",
                 "Write, review, refactor, and debug production-grade code", "💻",
                 "You are an elite software architect and systems programmer. Provide clean, secure, idiomatic, and robust code. Include concise explanations, handle edge cases, and format code with clear syntax blocks.", "all", 1),
                ("sk_proofread", "proofread", "Proofread & Polish",
                 "Enhance grammar, tone, flow, and vocabulary", "✨",
                 "You are a master editor. Review and polish the user's text for grammar, flow, clarity, and tone while preserving their authentic voice. Present the revised text first, followed by a brief summary of key improvements.", "all", 1),
                ("sk_brainstorm", "brainstorm", "Creative Brainstorm",
                 "Generate fresh ideas, angles, and creative solutions", "💡",
                 "You are a creative strategist and brainstormer. Generate a rich variety of novel, practical, and out-of-the-box ideas, perspectives, and alternative approaches for the user's objective.", "all", 1),
                ("sk_eli5", "eli5", "Explain Like I'm 5",
                 "Ultra-simple explanation with fun metaphors", "🧒",
                 "Explain the following topic as if the user is 5 years old. Use fun everyday metaphors, simple vocabulary, and zero complicated jargon.", "all", 1),
                ("sk_roast", "roast", "Witty Roast",
                 "Playfully roast an idea, code, or topic with sharp banter", "🔥",
                 "You are a sharp-witted stand-up comedian. Deliver a hilarious, clever, and playful roast of the user's topic, code, or idea with quick wit and zero malice.", "all", 1),
                ("sk_extract", "extract", "Extract Structured Data",
                 "Extract tables, JSON, key metrics, and action items", "📊",
                 "You are a data extraction specialist. Parse the user's text or attached document and extract key entities, numbers, dates, tasks, and data into clean Markdown tables or formatted JSON as appropriate.", "all", 1),
                ("sk_dictate", "dictate", "Voice Dictation",
                 "Convert speech to text with microphone", "🎙️",
                 "[ACTION:DICTATE]", "all", 1),
                ("sk_canvas", "canvas", "Canvas & Artifacts",
                 "Build interactive HTML/JS apps, tools, SVGs, or widgets for the Canvas", "🎨",
                 "You are an interactive app and artifact builder. Output complete, standalone, production-ready code (single-file HTML/CSS/JS, SVG, or clean script) designed to run seamlessly in the interactive Canvas sandbox.", "all", 1)
            ]
            c.executemany(
                "INSERT OR IGNORE INTO skills(id, command, name, description, icon, prompt, mode, is_builtin) VALUES(?,?,?,?,?,?,?,?)",
                default_skills
            )
        else:
            c.execute("""
                INSERT OR IGNORE INTO skills(id, command, name, description, icon, prompt, mode, is_builtin)
                VALUES('sk_canvas', 'canvas', 'Canvas & Artifacts',
                       'Build interactive HTML/JS apps, tools, SVGs, or widgets for the Canvas', '🎨',
                       'You are an interactive app and artifact builder. Output complete, standalone, production-ready code (single-file HTML/CSS/JS, SVG, or clean script) designed to run seamlessly in the interactive Canvas sandbox.',
                       'all', 1)
            """)

        # Migrate legacy messages without session_id into a default session per mode
        for m in MODES:
            unassigned = c.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE mode=? AND (session_id IS NULL OR session_id='')",
                (m,)
            ).fetchone()["n"]
            if unassigned > 0:
                legacy_id = f"legacy_{m}"
                c.execute(
                    "INSERT OR IGNORE INTO sessions(id, mode, title) VALUES(?, ?, ?)",
                    (legacy_id, m, "Main Conversation")
                )
                c.execute(
                    "UPDATE messages SET session_id=? WHERE mode=? AND (session_id IS NULL OR session_id='')",
                    (legacy_id, m)
                )


def get_setting(key: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def list_sessions(mode: str, workspace_id: str | None = None) -> list[dict]:
    with conn() as c:
        if workspace_id:
            rows = c.execute(
                """SELECT s.id, s.mode, s.title, s.workspace_id, s.created_at, s.updated_at,
                          COUNT(m.id) AS msg_count
                   FROM sessions s
                   LEFT JOIN messages m ON s.id = m.session_id
                   WHERE s.mode=? AND (s.workspace_id=? OR s.workspace_id IS NULL OR s.workspace_id='')
                   GROUP BY s.id
                   ORDER BY s.updated_at DESC, s.created_at DESC""",
                (mode, workspace_id)
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT s.id, s.mode, s.title, s.workspace_id, s.created_at, s.updated_at,
                          COUNT(m.id) AS msg_count
                   FROM sessions s
                   LEFT JOIN messages m ON s.id = m.session_id
                   WHERE s.mode=?
                   GROUP BY s.id
                   ORDER BY s.updated_at DESC, s.created_at DESC""",
                (mode,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_session(session_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(r) if r else None


def create_session(mode: str, title: str = "New Chat", session_id: str | None = None, workspace_id: str = "ws_default") -> str:
    sid = session_id or f"s_{uuid.uuid4().hex[:12]}"
    with conn() as c:
        c.execute(
            "INSERT INTO sessions(id, mode, title, workspace_id, updated_at) VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (sid, mode, title, workspace_id)
        )
    return sid


def get_or_create_latest_session(mode: str, workspace_id: str | None = None) -> str:
    with conn() as c:
        if workspace_id:
            r = c.execute(
                "SELECT id FROM sessions WHERE mode=? AND (workspace_id=? OR workspace_id IS NULL OR workspace_id='') ORDER BY updated_at DESC LIMIT 1",
                (mode, workspace_id)
            ).fetchone()
        else:
            r = c.execute(
                "SELECT id FROM sessions WHERE mode=? ORDER BY updated_at DESC LIMIT 1",
                (mode,)
            ).fetchone()
        if r:
            return r["id"]
    return create_session(mode, "New Chat", workspace_id=workspace_id or "ws_default")


def rename_session(session_id: str, title: str) -> None:
    clean = title.strip()[:80] or "Untitled Chat"
    with conn() as c:
        c.execute(
            "UPDATE sessions SET title=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (clean, session_id)
        )


def delete_session(session_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        c.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def get_session_summary(session_id: str) -> str:
    """Retrieve rolling compacted session summary for a given session."""
    if not session_id:
        return ""
    with conn() as c:
        try:
            r = c.execute("SELECT context_summary FROM sessions WHERE id=?", (session_id,)).fetchone()
            return (r["context_summary"] or "") if r else ""
        except Exception:
            return ""


def update_session_summary(session_id: str, summary: str) -> None:
    """Update rolling compacted session summary for a session."""
    if not session_id:
        return
    with conn() as c:
        try:
            c.execute("UPDATE sessions SET context_summary=? WHERE id=?", (summary.strip(), session_id))
        except Exception:
            pass


def add_message(mode: str, role: str, content: str,
                model: str | None = None, session_id: str | None = None,
                attachments: str | None = None, skill: str | None = None) -> None:
    if not session_id:
        session_id = get_or_create_latest_session(mode)
    with conn() as c:
        c.execute(
            "INSERT INTO messages(mode,role,content,model,session_id,attachments,skill) VALUES(?,?,?,?,?,?,?)",
            (mode, role, content, model, session_id, attachments, skill),
        )
        c.execute(
            "UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (session_id,)
        )
        # Auto-title if it's the first user message and session is still titled 'New Chat'
        if role == "user":
            curr = c.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
            if curr and curr["title"] in ("New Chat", "Main Conversation"):
                words = content.strip().splitlines()[0][:40].strip() if content.strip() else ""
                if not words and attachments:
                    words = "Attached File"
                if len(words) > 3:
                    auto_title = words if len(words) < 38 else words[:35] + "…"
                    c.execute("UPDATE sessions SET title=? WHERE id=?", (auto_title, session_id))


def history(mode: str, limit: int = 100, session_id: str | None = None) -> list[dict]:
    with conn() as c:
        if session_id:
            rows = c.execute(
                "SELECT role, content, created_at, model, attachments, skill FROM messages "
                "WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            sid = get_or_create_latest_session(mode)
            rows = c.execute(
                "SELECT role, content, created_at, model, attachments, skill FROM messages "
                "WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (sid, limit),
            ).fetchall()
    import json
    res = []
    for r in reversed(rows):
        d = dict(r)
        if d.get("attachments"):
            try:
                d["attachments"] = json.loads(d["attachments"])
            except Exception:
                d["attachments"] = None
        else:
            d["attachments"] = None
        res.append(d)
    return res


def clear_mode(mode: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM messages WHERE mode=?", (mode,))
        c.execute("DELETE FROM sessions WHERE mode=?", (mode,))


def get_profile() -> dict:
    with conn() as c:
        rows = c.execute("SELECT key, value FROM friend_profile").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_profile_field(key: str, value: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO friend_profile(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def clear_profile() -> None:
    with conn() as c:
        c.execute("DELETE FROM friend_profile")


def count_messages(mode: str, session_id: str | None = None) -> int:
    with conn() as c:
        if session_id:
            r = c.execute("SELECT COUNT(*) AS n FROM messages WHERE session_id=?",
                          (session_id,)).fetchone()
        else:
            r = c.execute("SELECT COUNT(*) AS n FROM messages WHERE mode=?",
                          (mode,)).fetchone()
        return r["n"]


def list_facts() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT id, fact FROM friend_facts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def add_fact(fact: str) -> int | None:
    fact = fact.strip()[:200]
    if not fact:
        return None
    with conn() as c:
        cur = c.execute("INSERT INTO friend_facts(fact) VALUES(?)", (fact,))
        return cur.lastrowid


def del_fact(fid: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM friend_facts WHERE id=?", (fid,))


def clear_facts() -> None:
    with conn() as c:
        c.execute("DELETE FROM friend_facts")


def pop_last_assistant(mode: str, session_id: str | None = None) -> bool:
    """Remove the latest message if it's an assistant reply. For regenerate."""
    with conn() as c:
        if session_id:
            last = c.execute(
                "SELECT id, role FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
                (session_id,)).fetchone()
        else:
            last = c.execute(
                "SELECT id, role FROM messages WHERE mode=? ORDER BY id DESC LIMIT 1",
                (mode,)).fetchone()
        if last and last["role"] == "assistant":
            c.execute("DELETE FROM messages WHERE id=?", (last["id"],))
            return True
    return False


def list_skills(mode: str | None = None) -> list[dict]:
    with conn() as c:
        if mode and mode != "all":
            rows = c.execute(
                "SELECT * FROM skills WHERE mode='all' OR mode=? ORDER BY is_builtin DESC, command ASC",
                (mode,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM skills ORDER BY is_builtin DESC, command ASC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_skill(skill_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        return dict(r) if r else None


def get_skill_by_command(cmd: str) -> dict | None:
    clean = cmd.lstrip("/").strip().lower()
    with conn() as c:
        r = c.execute("SELECT * FROM skills WHERE LOWER(command)=?", (clean,)).fetchone()
        return dict(r) if r else None


def create_skill(command: str, name: str, description: str, icon: str, prompt: str, mode: str = "all", is_builtin: int = 0) -> dict:
    clean_cmd = re.sub(r"[^a-zA-Z0-9_-]", "", command.lstrip("/").strip().lower())[:32]
    if not clean_cmd:
        raise ValueError("Invalid command trigger")
    sid = f"sk_{uuid.uuid4().hex[:10]}"
    clean_name = name.strip()[:64] or clean_cmd.title()
    clean_desc = description.strip()[:200] or ""
    clean_icon = icon.strip()[:10] or "⚡"
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise ValueError("Skill prompt directive cannot be empty")
    with conn() as c:
        c.execute(
            "INSERT INTO skills(id, command, name, description, icon, prompt, mode, is_builtin) VALUES(?,?,?,?,?,?,?,?)",
            (sid, clean_cmd, clean_name, clean_desc, clean_icon, clean_prompt, mode, is_builtin)
        )
    return get_skill(sid)


def update_skill(skill_id: str, command: str, name: str, description: str, icon: str, prompt: str, mode: str = "all") -> dict | None:
    existing = get_skill(skill_id)
    if not existing:
        return None
    clean_cmd = re.sub(r"[^a-zA-Z0-9_-]", "", command.lstrip("/").strip().lower())[:32]
    if not clean_cmd:
        raise ValueError("Invalid command trigger")
    clean_name = name.strip()[:64] or clean_cmd.title()
    clean_desc = description.strip()[:200] or ""
    clean_icon = icon.strip()[:10] or "⚡"
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise ValueError("Skill prompt directive cannot be empty")
    with conn() as c:
        c.execute(
            "UPDATE skills SET command=?, name=?, description=?, icon=?, prompt=?, mode=? WHERE id=?",
            (clean_cmd, clean_name, clean_desc, clean_icon, clean_prompt, mode, skill_id)
        )
    return get_skill(skill_id)


def delete_skill(skill_id: str) -> bool:
    with conn() as c:
        r = c.execute("SELECT is_builtin FROM skills WHERE id=?", (skill_id,)).fetchone()
        if not r:
            return False
        if r["is_builtin"] == 1:
            raise ValueError("Built-in skills cannot be deleted")
        c.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        return True


# ---------- Providers CRUD ----------
def list_providers(active_only: bool = False) -> list[dict]:
    with conn() as c:
        q = "SELECT * FROM providers" + (" WHERE is_active=1" if active_only else "") + " ORDER BY created_at ASC"
        return [dict(r) for r in c.execute(q).fetchall()]


def get_provider(provider_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        return dict(r) if r else None


def create_provider(name: str, provider_type: str, base_url: str, api_key: str = "") -> dict:
    pid = f"p_{uuid.uuid4().hex[:10]}"
    clean_name = name.strip()[:64] or "Custom Provider"
    clean_type = "openai_compat" if provider_type == "openai_compat" else "ollama"
    clean_url = base_url.strip().rstrip("/")
    with conn() as c:
        c.execute(
            "INSERT INTO providers(id, name, provider_type, base_url, api_key, is_active) VALUES(?,?,?,?,?,1)",
            (pid, clean_name, clean_type, clean_url, api_key.strip())
        )
    return get_provider(pid)


def update_provider(provider_id: str, name: str | None = None, base_url: str | None = None,
                    api_key: str | None = None, is_active: int | None = None) -> dict | None:
    cur = get_provider(provider_id)
    if not cur:
        return None
    new_name = name.strip()[:64] if name is not None else cur["name"]
    new_url = base_url.strip().rstrip("/") if base_url is not None else cur["base_url"]
    new_key = api_key.strip() if api_key is not None else cur["api_key"]
    new_act = int(bool(is_active)) if is_active is not None else cur["is_active"]
    with conn() as c:
        c.execute(
            "UPDATE providers SET name=?, base_url=?, api_key=?, is_active=? WHERE id=?",
            (new_name, new_url, new_key, new_act, provider_id)
        )
    return get_provider(provider_id)


def delete_provider(provider_id: str) -> bool:
    if provider_id == "p_ollama":
        raise ValueError("Cannot delete native default Ollama provider")
    with conn() as c:
        c.execute("DELETE FROM providers WHERE id=?", (provider_id,))
        return True


# ---------- Model Experiments & Runs ----------
def create_experiment(exp_id: str, prompt: str, suite_name: str = "",
                      mode: str = "transparent", execution_mode: str = "sequential") -> dict:
    with conn() as c:
        c.execute(
            "INSERT INTO model_experiments(id, suite_name, prompt, mode, execution_mode) VALUES(?,?,?,?,?)",
            (exp_id, suite_name[:100], prompt, mode, execution_mode)
        )
    return get_experiment(exp_id)


def add_experiment_run(exp_id: str, model_name: str, provider_id: str,
                       response_text: str, tokens_prompt: int, tokens_completion: int,
                       duration_ms: int, ttft_ms: int, tokens_per_sec: float,
                       vram_delta_mb: float = 0.0) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO model_experiment_runs(
                experiment_id, model_name, provider_id, response_text,
                tokens_prompt, tokens_completion, duration_ms, ttft_ms,
                tokens_per_sec, vram_delta_mb
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (exp_id, model_name, provider_id, response_text,
             tokens_prompt, tokens_completion, duration_ms, ttft_ms,
             round(tokens_per_sec, 2), round(vram_delta_mb, 1))
        )
        return cur.lastrowid


def get_experiment(exp_id: str) -> dict | None:
    with conn() as c:
        exp = c.execute("SELECT * FROM model_experiments WHERE id=?", (exp_id,)).fetchone()
        if not exp:
            return None
        out = dict(exp)
        runs = c.execute("SELECT * FROM model_experiment_runs WHERE experiment_id=? ORDER BY id ASC", (exp_id,)).fetchall()
        out["runs"] = [dict(r) for r in runs]
        return out


def list_experiments(limit: int = 50) -> list[dict]:
    with conn() as c:
        exps = c.execute("SELECT * FROM model_experiments ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for e in exps:
            item = dict(e)
            runs = c.execute("SELECT model_name, tokens_per_sec, duration_ms, rating, user_voted FROM model_experiment_runs WHERE experiment_id=?", (e["id"],)).fetchall()
            item["runs_summary"] = [dict(r) for r in runs]
            result.append(item)
        return result


def vote_experiment(exp_id: str, winner_model: str, ratings: dict[str, int] | None = None) -> dict | None:
    with conn() as c:
        c.execute("UPDATE model_experiments SET winner_model=? WHERE id=?", (winner_model, exp_id))
        c.execute("UPDATE model_experiment_runs SET user_voted = (CASE WHEN model_name=? THEN 1 ELSE 0 END) WHERE experiment_id=?", (winner_model, exp_id))
        if ratings:
            for model_name, stars in ratings.items():
                c.execute("UPDATE model_experiment_runs SET rating=? WHERE experiment_id=? AND model_name=?", (int(stars), exp_id, model_name))
    return get_experiment(exp_id)


# ---------- Benchmark Prompt Suites ----------
def list_lab_suites() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM lab_prompt_suites ORDER BY is_builtin DESC, category ASC, name ASC").fetchall()]


def create_lab_suite(name: str, category: str, prompt: str) -> dict:
    sid = f"suite_{uuid.uuid4().hex[:10]}"
    clean_name = name.strip()[:80] or "Custom Suite"
    clean_cat = category.strip()[:40] or "General"
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise ValueError("Prompt cannot be empty")
    with conn() as c:
        c.execute(
            "INSERT INTO lab_prompt_suites(id, name, category, prompt, is_builtin) VALUES(?,?,?,?,0)",
            (sid, clean_name, clean_cat, clean_prompt)
        )
        r = c.execute("SELECT * FROM lab_prompt_suites WHERE id=?", (sid,)).fetchone()
        return dict(r)


def delete_lab_suite(suite_id: str) -> bool:
    with conn() as c:
        r = c.execute("SELECT is_builtin FROM lab_prompt_suites WHERE id=?", (suite_id,)).fetchone()
        if not r:
            return False
        if r["is_builtin"] == 1:
            raise ValueError("Built-in suites cannot be deleted")
        c.execute("DELETE FROM lab_prompt_suites WHERE id=?", (suite_id,))
        return True


# ---------- Arena Sessions (Council & Debate) ----------
def create_arena_session(arena_type: str, title: str, topic: str, judge_model: str,
                         models: list, rounds: list, verdict_text: str, verdict_json: dict) -> dict:
    import json as _json
    sid = f"arena_{arena_type}_{uuid.uuid4().hex[:10]}"
    clean_title = (title.strip()[:100] or f"AI {arena_type.capitalize()} Session")
    clean_topic = topic.strip()
    m_json = _json.dumps(models)
    r_json = _json.dumps(rounds)
    v_json = _json.dumps(verdict_json or {})
    with conn() as c:
        c.execute(
            """INSERT INTO arena_sessions(id, arena_type, title, topic, judge_model, models_json, rounds_json, verdict_text, verdict_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (sid, arena_type, clean_title, clean_topic, judge_model, m_json, r_json, verdict_text, v_json)
        )
    return get_arena_session(sid)


def list_arena_sessions(arena_type: str | None = None, limit: int = 50) -> list[dict]:
    import json as _json
    with conn() as c:
        if arena_type:
            rows = c.execute(
                "SELECT id, arena_type, title, topic, judge_model, models_json, verdict_text, created_at FROM arena_sessions WHERE arena_type=? ORDER BY created_at DESC LIMIT ?",
                (arena_type, limit)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, arena_type, title, topic, judge_model, models_json, verdict_text, created_at FROM arena_sessions ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["models"] = _json.loads(d.get("models_json") or "[]")
            except Exception:
                d["models"] = []
            result.append(d)
        return result


def get_arena_session(session_id: str) -> dict | None:
    import json as _json
    with conn() as c:
        row = c.execute("SELECT * FROM arena_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["models"] = _json.loads(d.get("models_json") or "[]")
        except Exception:
            d["models"] = []
        try:
            d["rounds"] = _json.loads(d.get("rounds_json") or "[]")
        except Exception:
            d["rounds"] = []
        try:
            d["verdict_json"] = _json.loads(d.get("verdict_json") or "{}")
        except Exception:
            d["verdict_json"] = {}
        return d


def delete_arena_session(session_id: str) -> bool:
    with conn() as c:
        c.execute("DELETE FROM arena_sessions WHERE id=?", (session_id,))
        return True


# ==========================================================================
# Document Hub (Library Documents & Knowledge)
# ==========================================================================
def save_library_document(doc_id: str, filename: str, file_type: str, file_size: int,
                          preview_url: str, char_count: int = 0, page_count: int | None = None,
                          extracted_text: str = "", summary: str = "") -> dict:
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO library_documents
               (id, filename, file_type, file_size, preview_url, char_count, page_count, extracted_text, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, filename, file_type, file_size, preview_url, char_count, page_count, extracted_text, summary)
        )
    return get_library_document(doc_id)


def list_library_documents(limit: int = 100) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT id, filename, file_type, file_size, preview_url, char_count, page_count, summary, created_at
               FROM library_documents ORDER BY created_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_library_document(doc_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM library_documents WHERE id=?", (doc_id,)).fetchone()
        return dict(row) if row else None


def delete_library_document(doc_id: str) -> bool:
    with conn() as c:
        c.execute("DELETE FROM library_documents WHERE id=?", (doc_id,))
        return True


def update_library_document_summary(doc_id: str, summary: str) -> bool:
    with conn() as c:
        c.execute("UPDATE library_documents SET summary=? WHERE id=?", (summary, doc_id))
        return True


# ==========================================================================
# Canvas Artifacts & Snapshots
# ==========================================================================
def save_canvas_artifact(title: str, lang: str, code: str, parent_id: str | None = None, version: int = 1) -> dict:
    aid = f"art_{uuid.uuid4().hex[:10]}"
    clean_title = (title.strip()[:80] or "Untitled Artifact")
    with conn() as c:
        c.execute(
            """INSERT INTO canvas_artifacts (id, title, lang, code, version, parent_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (aid, clean_title, lang.lower(), code, version, parent_id)
        )
    return get_canvas_artifact(aid)


def list_canvas_artifacts(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT id, title, lang, version, parent_id, created_at, length(code) AS code_len
               FROM canvas_artifacts ORDER BY created_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_canvas_artifact(artifact_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM canvas_artifacts WHERE id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None


def delete_canvas_artifact(artifact_id: str) -> bool:
    with conn() as c:
        c.execute("DELETE FROM canvas_artifacts WHERE id=?", (artifact_id,))
        return True


# ==========================================================================
# Model Observatory & Inference Telemetry
# ==========================================================================
def log_inference(model: str, provider: str = "ollama", prompt_chars: int = 0,
                  response_chars: int = 0, duration_ms: int = 0, ttft_ms: int = 0,
                  tokens_per_sec: float = 0.0, status: str = "ok",
                  error_msg: str | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO inference_logs
               (model, provider, prompt_chars, response_chars, duration_ms, ttft_ms, tokens_per_sec, status, error_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (model, provider, prompt_chars, response_chars, duration_ms, ttft_ms, round(tokens_per_sec, 2), status, error_msg)
        )
        return cur.lastrowid or 0


def get_inference_logs(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT * FROM inference_logs ORDER BY created_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def clear_inference_logs() -> bool:
    with conn() as c:
        c.execute("DELETE FROM inference_logs")
        return True


def get_inference_stats() -> dict:
    with conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM inference_logs").fetchone()["n"]
        if total == 0:
            return {
                "total_inferences": 0,
                "avg_tokens_per_sec": 0.0,
                "avg_duration_ms": 0,
                "error_count": 0,
                "success_rate": 100.0
            }
        errors = c.execute("SELECT COUNT(*) AS n FROM inference_logs WHERE status='error'").fetchone()["n"]
        row = c.execute(
            """SELECT AVG(tokens_per_sec) AS avg_tps, AVG(duration_ms) AS avg_dur
               FROM inference_logs WHERE status='ok'"""
        ).fetchone()
        avg_tps = round(row["avg_tps"] or 0.0, 2)
        avg_dur = int(round(row["avg_dur"] or 0))
        success_rate = round(((total - errors) / total) * 100, 1) if total > 0 else 100.0
        return {
            "total_inferences": total,
            "avg_tokens_per_sec": avg_tps,
            "avg_duration_ms": avg_dur,
            "error_count": errors,
            "success_rate": success_rate
        }


# ==========================================================================
# Reusable Prompt Library
# ==========================================================================
def save_prompt_template(title: str, user_template: str, description: str = "",
                         category: str = "General", system_prompt: str = "",
                         is_favorite: int = 0, template_id: str | None = None) -> dict:
    tid = template_id or f"pt_{uuid.uuid4().hex[:10]}"
    clean_title = title.strip()[:100] or "Untitled Template"
    clean_cat = category.strip()[:40] or "General"
    with conn() as c:
        existing = c.execute("SELECT version FROM prompt_templates WHERE id=?", (tid,)).fetchone()
        if existing:
            new_version = (existing["version"] or 1) + 1
            c.execute(
                """UPDATE prompt_templates
                   SET title=?, description=?, category=?, system_prompt=?, user_template=?,
                       version=?, is_favorite=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (clean_title, description.strip(), clean_cat, system_prompt.strip(), user_template.strip(),
                 new_version, is_favorite, tid)
            )
        else:
            c.execute(
                """INSERT INTO prompt_templates
                   (id, title, description, category, system_prompt, user_template, version, is_builtin, is_favorite)
                   VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?)""",
                (tid, clean_title, description.strip(), clean_cat, system_prompt.strip(), user_template.strip(), is_favorite)
            )
    return get_prompt_template(tid) or {}


def list_prompt_templates(category: str | None = None, favorites_only: bool = False) -> list[dict]:
    with conn() as c:
        query = "SELECT * FROM prompt_templates WHERE 1=1"
        params = []
        if category and category.lower() != "all":
            query += " AND lower(category) = lower(?)"
            params.append(category)
        if favorites_only:
            query += " AND is_favorite = 1"
        query += " ORDER BY is_favorite DESC, is_builtin DESC, updated_at DESC"
        rows = c.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_prompt_template(template_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM prompt_templates WHERE id=?", (template_id,)).fetchone()
        return dict(row) if row else None


def delete_prompt_template(template_id: str) -> bool:
    with conn() as c:
        c.execute("DELETE FROM prompt_templates WHERE id=?", (template_id,))
        return True


def toggle_prompt_favorite(template_id: str) -> bool:
    with conn() as c:
        row = c.execute("SELECT is_favorite FROM prompt_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            return False
        new_fav = 0 if row["is_favorite"] else 1
        c.execute("UPDATE prompt_templates SET is_favorite=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_fav, template_id))
        return bool(new_fav)


# ==============================================================================
# Project Workspaces CRUD
# ==============================================================================

def list_workspaces() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT w.*,
                      (SELECT COUNT(*) FROM sessions s WHERE s.workspace_id = w.id) AS session_count,
                      (SELECT COUNT(*) FROM memories m WHERE m.workspace_id = w.id AND m.is_active = 1) AS memory_count
               FROM workspaces w
               ORDER BY w.is_default DESC, w.updated_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_workspace(ws_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        return dict(row) if row else None


def get_workspace_by_slug(slug: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
        return dict(row) if row else None


def create_workspace(name: str, slug: str | None = None, description: str = "",
                     instructions: str = "", icon: str = "📁", color: str = "#38bdf8",
                     repo_path: str = "", model_pref: str = "") -> dict:
    ws_id = f"ws_{uuid.uuid4().hex[:10]}"
    final_slug = slug or re.sub(r'[^a-z0-9_-]', '', name.lower().replace(' ', '-')) or ws_id
    with conn() as c:
        # Check uniqueness of slug
        existing = c.execute("SELECT id FROM workspaces WHERE slug=?", (final_slug,)).fetchone()
        if existing:
            final_slug = f"{final_slug}-{uuid.uuid4().hex[:4]}"
        c.execute(
            """INSERT INTO workspaces(id, name, slug, description, instructions, icon, color, repo_path, model_pref)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ws_id, name.strip(), final_slug, description.strip(), instructions.strip(), icon, color, repo_path.strip(), model_pref.strip())
        )
    return get_workspace(ws_id)


def update_workspace(ws_id: str, name: str | None = None, description: str | None = None,
                     instructions: str | None = None, icon: str | None = None, color: str | None = None,
                     repo_path: str | None = None, model_pref: str | None = None) -> dict | None:
    fields = []
    values = []
    if name is not None:
        fields.append("name = ?")
        values.append(name.strip())
    if description is not None:
        fields.append("description = ?")
        values.append(description.strip())
    if instructions is not None:
        fields.append("instructions = ?")
        values.append(instructions.strip())
    if icon is not None:
        fields.append("icon = ?")
        values.append(icon.strip())
    if color is not None:
        fields.append("color = ?")
        values.append(color.strip())
    if repo_path is not None:
        fields.append("repo_path = ?")
        values.append(repo_path.strip())
    if model_pref is not None:
        fields.append("model_pref = ?")
        values.append(model_pref.strip())

    if not fields:
        return get_workspace(ws_id)

    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(ws_id)

    with conn() as c:
        c.execute(f"UPDATE workspaces SET {', '.join(fields)} WHERE id = ?", tuple(values))
    return get_workspace(ws_id)


def delete_workspace(ws_id: str) -> bool:
    if ws_id == "ws_default":
        return False  # Cannot delete default workspace
    with conn() as c:
        # Reassign sessions to default workspace to preserve history
        c.execute("UPDATE sessions SET workspace_id = 'ws_default' WHERE workspace_id = ?", (ws_id,))
        # Move memories to default or delete workspace-specific ones
        c.execute("DELETE FROM memories WHERE workspace_id = ?", (ws_id,))
        c.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
        return True


# ==============================================================================
# Unified Memory Control Center CRUD
# ==============================================================================

def list_memories(workspace_id: str | None = None, scope: str | None = None,
                  category: str | None = None, search: str | None = None,
                  active_only: bool = False) -> list[dict]:
    with conn() as c:
        query = "SELECT m.*, w.name AS workspace_name FROM memories m LEFT JOIN workspaces w ON m.workspace_id = w.id WHERE 1=1"
        params = []
        if active_only:
            query += " AND m.is_active = 1"
        if scope:
            query += " AND m.scope = ?"
            params.append(scope)
        if workspace_id:
            query += " AND (m.workspace_id = ? OR m.scope = 'global')"
            params.append(workspace_id)
        if category:
            query += " AND m.category = ?"
            params.append(category)
        if search:
            query += " AND m.content LIKE ?"
            params.append(f"%{search.strip()}%")
        query += " ORDER BY m.scope DESC, m.updated_at DESC"
        rows = c.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_memory(memory_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return dict(row) if row else None


def create_memory(content: str, workspace_id: str | None = None, scope: str = "global",
                  category: str = "fact", source_session_id: str | None = None,
                  source_message_id: int | None = None, confidence: float = 1.0) -> dict:
    mem_id = f"mem_{uuid.uuid4().hex[:10]}"
    final_scope = "workspace" if (workspace_id and scope == "workspace") else "global"
    target_ws = workspace_id if final_scope == "workspace" else None
    with conn() as c:
        c.execute(
            """INSERT INTO memories(id, workspace_id, scope, category, content, source_session_id, source_message_id, confidence, is_active)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (mem_id, target_ws, final_scope, category, content.strip(), source_session_id, source_message_id, confidence)
        )
    return get_memory(mem_id)


def update_memory(memory_id: str, content: str | None = None, category: str | None = None,
                  is_active: int | None = None, scope: str | None = None,
                  workspace_id: str | None = None) -> dict | None:
    fields = []
    values = []
    if content is not None:
        fields.append("content = ?")
        values.append(content.strip())
    if category is not None:
        fields.append("category = ?")
        values.append(category.strip())
    if is_active is not None:
        fields.append("is_active = ?")
        values.append(int(is_active))
    if scope is not None:
        fields.append("scope = ?")
        values.append(scope)
    if workspace_id is not None:
        fields.append("workspace_id = ?")
        values.append(workspace_id if workspace_id != "" else None)

    if not fields:
        return get_memory(memory_id)

    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(memory_id)

    with conn() as c:
        c.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", tuple(values))
    return get_memory(memory_id)


def delete_memory(memory_id: str) -> bool:
    with conn() as c:
        c.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return True


def get_relevant_memories_for_context(workspace_id: str | None = None, limit: int = 25) -> list[dict]:
    with conn() as c:
        if workspace_id and workspace_id != "ws_default":
            rows = c.execute(
                """SELECT id, scope, category, content, workspace_id
                   FROM memories
                   WHERE is_active = 1 AND (scope = 'global' OR workspace_id = ?)
                   ORDER BY scope DESC, updated_at DESC LIMIT ?""",
                (workspace_id, limit)
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT id, scope, category, content, workspace_id
                   FROM memories
                   WHERE is_active = 1 AND (scope = 'global' OR workspace_id = 'ws_default')
                   ORDER BY scope DESC, updated_at DESC LIMIT ?""",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ==============================================================================
# Database Backup Snapshots Engine
# ==============================================================================

def ensure_backups_dir() -> Path:
    bdir = Path(__file__).parent / "backups"
    bdir.mkdir(exist_ok=True)
    return bdir


def create_backup_snapshot(trigger_type: str = "manual") -> dict | None:
    import datetime
    bdir = ensure_backups_dir()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_id = f"snap_{uuid.uuid4().hex[:8]}"
    filename = f"chatbots_backup_{ts}.db"
    dest_path = bdir / filename

    try:
        with conn() as c:
            # Atomic SQLite online vacuum into destination file
            c.execute(f"VACUUM INTO '{dest_path.as_posix()}'")
            size = dest_path.stat().st_size
            c.execute(
                "INSERT INTO backup_snapshots(id, filename, filepath, file_size, trigger_type) VALUES(?, ?, ?, ?, ?)",
                (snap_id, filename, dest_path.as_posix(), size, trigger_type)
            )
            # Prune older backups if exceeding 10
            rows = c.execute("SELECT id, filepath FROM backup_snapshots ORDER BY created_at DESC").fetchall()
            if len(rows) > 10:
                for old in rows[10:]:
                    try:
                        p = Path(old["filepath"])
                        if p.exists():
                            p.unlink()
                    except Exception:
                        pass
                    c.execute("DELETE FROM backup_snapshots WHERE id=?", (old["id"],))

        return {
            "id": snap_id,
            "filename": filename,
            "filepath": dest_path.as_posix(),
            "file_size": size,
            "trigger_type": trigger_type
        }
    except Exception as e:
        print(f"[Backup] Failed to create backup snapshot: {e}")
        return None


def list_backup_snapshots(limit: int = 20) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM backup_snapshots ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_backup_snapshot(snapshot_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM backup_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return dict(row) if row else None


def delete_backup_snapshot(snapshot_id: str) -> bool:
    with conn() as c:
        row = c.execute("SELECT filepath FROM backup_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row:
            try:
                p = Path(row["filepath"])
                if p.exists():
                    p.unlink()
            except Exception:
                pass
            c.execute("DELETE FROM backup_snapshots WHERE id=?", (snapshot_id,))
            return True
        return False




