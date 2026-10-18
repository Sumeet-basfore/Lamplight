"""
Lamplight Workflow Recipes & Automation Engine (Pillar 7)
=========================================================
Provides:
1. Workflow Recipes Engine: Reusable multi-step pipelines combining LLMs, local tools,
   document retrieval, code inspection, and persistent artifacts.
2. Persistent Background Job Queue: Asynchronous worker pool with crash recovery,
   live progress tracking, cancellation, and step logging.
3. Scheduled Local Tasks Engine: Interval and daily automated schedulers for
   reindexing, repository audits, flashcard reviews, and backups.
4. Tool & Connector Registry: Central registry for Git, filesystem, terminal execution,
   notes vaults, and HTTP/MCP services with fine-grained bot permissions.
"""

import os
import re
import json
import time
import uuid
import shutil
import sqlite3
import asyncio
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "chatbots.db"
BACKUP_DIR = BASE / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

logger = logging.getLogger("automation_workflows")

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============================================================================
# DATABASE INITIALIZATION
# ============================================================================

def init_automation_tables():
    """Initializes tables for workflow recipes, background jobs, scheduled tasks, and connectors."""
    with _get_db() as conn:
        c = conn.cursor()

        # 1. Workflow Recipes Table
        c.execute("""
            CREATE TABLE IF NOT EXISTS workflow_recipes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                icon TEXT DEFAULT 'zap',
                category TEXT DEFAULT 'general',
                steps_json TEXT NOT NULL,
                input_schema_json TEXT DEFAULT '[]',
                is_preset INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Background Job Queue Table
        c.execute("""
            CREATE TABLE IF NOT EXISTS background_jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                job_type TEXT NOT NULL,
                recipe_id TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER DEFAULT 0,
                progress_label TEXT DEFAULT '',
                model_name TEXT DEFAULT '',
                params_json TEXT DEFAULT '{}',
                result_json TEXT DEFAULT '{}',
                logs_json TEXT DEFAULT '[]',
                error TEXT,
                cancel_requested INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                started_at DATETIME,
                completed_at DATETIME
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bgjobs_status ON background_jobs(status, created_at)")

        # 3. Scheduled Tasks Table
        c.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                schedule_type TEXT DEFAULT 'interval',
                interval_minutes INTEGER DEFAULT 60,
                daily_time TEXT DEFAULT '09:00',
                task_type TEXT NOT NULL,
                recipe_id TEXT,
                params_json TEXT DEFAULT '{}',
                enabled INTEGER DEFAULT 1,
                last_run_at DATETIME,
                next_run_at DATETIME,
                last_status TEXT DEFAULT 'idle',
                last_error TEXT,
                run_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 4. Tool & Connector Registry Table
        c.execute("""
            CREATE TABLE IF NOT EXISTS tool_connectors (
                id TEXT PRIMARY KEY,
                connector_type TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT DEFAULT 'core',
                operations_json TEXT DEFAULT '[]',
                permissions_json TEXT DEFAULT '[]',
                approval_required INTEGER DEFAULT 0,
                allowed_bots_json TEXT DEFAULT '["*"]',
                enabled INTEGER DEFAULT 1,
                config_json TEXT DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()

    # Seed default recipes and connectors
    _seed_default_recipes()
    _seed_default_connectors()
    _seed_default_schedules()
    _recover_stale_jobs()


def _recover_stale_jobs():
    """Recovers any jobs marked 'running' across server restarts to 'failed'."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE background_jobs
            SET status = 'failed',
                error = 'Interrupted by server restart',
                completed_at = CURRENT_TIMESTAMP
            WHERE status = 'running'
        """)
        conn.commit()


# ============================================================================
# SEED DEFAULTS: RECIPES, CONNECTORS & SCHEDULES
# ============================================================================

def _seed_default_recipes():
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM workflow_recipes WHERE is_preset = 1")
        if c.fetchone()["cnt"] > 0:
            return

        presets = [
            {
                "id": "recipe_deep_research",
                "title": "Deep Research & Sourced Report",
                "description": "Multi-step research pipeline: extracts claims, runs comparative analysis, and compiles an evidence-backed report.",
                "icon": "book-open",
                "category": "research",
                "input_schema": [
                    {"key": "topic", "label": "Research Topic / Question", "type": "text", "default": "Local LLM Fine-Tuning Efficiency"},
                    {"key": "focus_area", "label": "Key Focus Area or Constraint", "type": "text", "default": "Quantization (GGUF vs EXL2), VRAM limits, and hardware benchmarks"},
                    {"key": "model", "label": "LLM Model", "type": "model", "default": "Qwen3.5-uncensored:latest"}
                ],
                "steps": [
                    {
                        "id": "step_extract",
                        "name": "Knowledge & Source Gathering",
                        "type": "llm_generate",
                        "prompt": "Identify 4-6 primary technical aspects, foundational hypotheses, and empirical metrics needed to thoroughly answer the following research question:\nTopic: {{inputs.topic}}\nFocus: {{inputs.focus_area}}\n\nProvide clear bullet points of core claims and theoretical considerations."
                    },
                    {
                        "id": "step_synthesis",
                        "name": "Comparative Critical Analysis",
                        "type": "llm_generate",
                        "prompt": "Act as a rigorous senior research scientist. Critically analyze and stress-test the following preliminary research findings:\n\n{{steps.step_extract.output}}\n\nIdentify potential trade-offs, edge-case limitations, contradictory evidence, and hardware bottlenecks. Structure your critique clearly."
                    },
                    {
                        "id": "step_report",
                        "name": "Final Sourced Report Synthesis",
                        "type": "llm_generate",
                        "prompt": "Synthesize the extracted claims and critical evaluation into an authoritative Markdown research report on '{{inputs.topic}}'.\n\nContext & Critique:\n{{steps.step_synthesis.output}}\n\nInclude:\n1. Executive Summary\n2. Key Technical Findings & Benchmarks\n3. Nuances & Trade-offs\n4. Recommended Next Steps or Experiments\nFormat with clean Markdown headers, tables, and bullet points."
                    }
                ]
            },
            {
                "id": "recipe_dev_review",
                "title": "Dev Review & Test Case Generator",
                "description": "Inspects git diff and project state, generates targeted unit tests, and summarizes regression risks.",
                "icon": "git-pull-request",
                "category": "engineering",
                "input_schema": [
                    {"key": "scope", "label": "Review Scope / Component", "type": "text", "default": "Recent changes and unit tests"},
                    {"key": "framework", "label": "Test Framework", "type": "text", "default": "pytest"},
                    {"key": "model", "label": "LLM Model", "type": "model", "default": "qwen2.5-coder:latest"}
                ],
                "steps": [
                    {
                        "id": "step_inspect_git",
                        "name": "Git Status & Diff Analysis",
                        "type": "git_diff_inspect",
                        "prompt": "Inspect repository status and modified files for scope: {{inputs.scope}}."
                    },
                    {
                        "id": "step_gen_tests",
                        "name": "Targeted Test Case Generation",
                        "type": "llm_generate",
                        "prompt": "You are a lead QA and Software Architect. Given the following repository diff and recent changes:\n\n{{steps.step_inspect_git.output}}\n\nGenerate comprehensive test cases using {{inputs.framework}}. Cover:\n1. Normal positive flows\n2. Edge cases (null/empty inputs, malformed structures, timeouts)\n3. Potential regression points\nProvide runnable, idiomatic test code."
                    },
                    {
                        "id": "step_audit_summary",
                        "name": "Regression Risk Assessment",
                        "type": "llm_generate",
                        "prompt": "Evaluate the risk level of the changes reviewed above. Outline:\n- Potential breaking changes or backward-incompatibility risks\n- Performance/memory impact\n- Verification checklist before merging"
                    }
                ]
            },
            {
                "id": "recipe_creative_draft",
                "title": "Creative Scene Drafting & Lore Audit",
                "description": "Pulls story bible canon, drafts a vivid narrative scene, and executes a continuity check against world rules.",
                "icon": "feather",
                "category": "creative",
                "input_schema": [
                    {"key": "characters", "label": "Characters Involved", "type": "text", "default": "Caelen (Rogue Archon), Lyra (Scribe)"},
                    {"key": "location", "label": "Scene Setting / Location", "type": "text", "default": "The Grand Archive of Oakhaven at midnight"},
                    {"key": "conflict", "label": "Core Conflict / Event", "type": "text", "default": "Uncovering a sealed cipher that reveals a treasonous alliance"},
                    {"key": "model", "label": "LLM Model", "type": "model", "default": "Qwen3.5-uncensored:latest"}
                ],
                "steps": [
                    {
                        "id": "step_draft_scene",
                        "name": "Narrative Scene Generation",
                        "type": "llm_generate",
                        "prompt": "Write a compelling, atmospheric narrative scene featuring:\nCharacters: {{inputs.characters}}\nLocation: {{inputs.location}}\nPlot Event: {{inputs.conflict}}\n\nFocus on tight pacing, sensory descriptions, distinct character voices, and underlying dramatic tension."
                    },
                    {
                        "id": "step_audit_lore",
                        "name": "Lore Continuity & Character Consistency Audit",
                        "type": "llm_generate",
                        "prompt": "Act as a meticulous narrative continuity editor. Audit the drafted scene:\n\n{{steps.step_draft_scene.output}}\n\nEvaluate:\n1. Character Consistency: Do character actions align with their established motives?\n2. Spatial & Temporal Logic: Are movements and scene transitions coherent?\n3. Pacing & Tone Polish: Highlight 2-3 specific lines that could be elevated or sharpened."
                    }
                ]
            },
            {
                "id": "recipe_audio_distill",
                "title": "Audio Transcript & Meeting Distillation",
                "description": "Processes conversational or meeting transcripts into action items, key decisions, and study flashcards.",
                "icon": "headphones",
                "category": "batch",
                "input_schema": [
                    {"key": "transcript_snippet", "label": "Transcript Content / Meeting Notes", "type": "textarea", "default": "Speaker 1: We decided to deploy the new SQLite schema migrations tomorrow morning at 09:00 UTC. Sumeet will supervise the initial rollout. We also need to add cancellation tokens to the worker loop by Thursday to avoid orphaned processes."},
                    {"key": "model", "label": "LLM Model", "type": "model", "default": "Qwen3.5-uncensored:latest"}
                ],
                "steps": [
                    {
                        "id": "step_actions",
                        "name": "Action Items & Decision Matrix",
                        "type": "llm_generate",
                        "prompt": "Analyze the following transcript:\n{{inputs.transcript_snippet}}\n\nExtract:\n1. Key Decisions Made\n2. Action Items (with Owner, Deadline if mentioned, and Priority)\n3. Unresolved Questions or Pending Blockers\nFormat cleanly in Markdown."
                    },
                    {
                        "id": "step_flashcards",
                        "name": "Flashcard Study Generation",
                        "type": "llm_generate",
                        "prompt": "Based on the decisions and concepts in this transcript:\n{{inputs.transcript_snippet}}\n\nCreate 3 high-yield question & answer flashcard pairs suitable for spaced repetition study. Format as:\nQ: [Question]\nA: [Answer]"
                    }
                ]
            }
        ]

        for p in presets:
            c.execute("""
                INSERT INTO workflow_recipes (id, title, description, icon, category, steps_json, input_schema_json, is_preset)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                p["id"],
                p["title"],
                p["description"],
                p["icon"],
                p["category"],
                json.dumps(p["steps"]),
                json.dumps(p["input_schema"])
            ))
        conn.commit()


def _seed_default_connectors():
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM tool_connectors")
        if c.fetchone()["cnt"] > 0:
            return

        connectors = [
            {
                "id": "conn_filesystem",
                "connector_type": "filesystem",
                "name": "Local Filesystem Connector",
                "description": "Controlled read and write access to project workspaces and user uploads.",
                "category": "core",
                "operations": ["read_file", "write_file", "list_directory", "inspect_metadata"],
                "permissions": ["fs:read", "fs:write_workspace"],
                "approval_required": 0,
                "allowed_bots": ["*"]
            },
            {
                "id": "conn_git",
                "connector_type": "git",
                "name": "Git Repository Intelligence",
                "description": "Read status, diffs, branch logs, and generate patches across local Git repositories.",
                "category": "core",
                "operations": ["git_status", "git_diff", "git_log", "git_branch"],
                "permissions": ["git:read", "git:diff"],
                "approval_required": 0,
                "allowed_bots": ["architect", "friend"]
            },
            {
                "id": "conn_terminal",
                "connector_type": "terminal",
                "name": "Safe Sandboxed Terminal",
                "description": "Controlled subprocess execution with timeouts, output caps, and working-directory restrictions.",
                "category": "system",
                "operations": ["run_python", "run_pytest", "run_linter", "inspect_process"],
                "permissions": ["shell:sandbox_exec"],
                "approval_required": 1,
                "allowed_bots": ["architect"]
            },
            {
                "id": "conn_notes_vault",
                "connector_type": "notes_vault",
                "name": "Markdown Notes Vault Watcher",
                "description": "Indexes Markdown notes directories, extracts headings and frontmatter, and updates FTS search index.",
                "category": "knowledge",
                "operations": ["sync_vault", "search_vault", "read_note", "append_note"],
                "permissions": ["vault:read", "vault:write"],
                "approval_required": 0,
                "allowed_bots": ["*"]
            },
            {
                "id": "conn_http_services",
                "connector_type": "http_services",
                "name": "Local Services & HTTP Endpoints",
                "description": "Integration bridge to local inference servers (Ollama, ComfyUI, Kokoro TTS).",
                "category": "network",
                "operations": ["check_health", "query_tags", "dispatch_generation"],
                "permissions": ["net:localhost_only"],
                "approval_required": 0,
                "allowed_bots": ["*"]
            },
            {
                "id": "conn_mcp",
                "connector_type": "mcp",
                "name": "Model Context Protocol (MCP) Bridge",
                "description": "Extensible connector for external Model Context Protocol server tools and resources.",
                "category": "integrations",
                "operations": ["list_tools", "call_tool", "read_resource"],
                "permissions": ["mcp:call"],
                "approval_required": 1,
                "allowed_bots": ["architect", "muse"]
            }
        ]

        for conn_item in connectors:
            c.execute("""
                INSERT INTO tool_connectors (id, connector_type, name, description, category, operations_json, permissions_json, approval_required, allowed_bots_json, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                conn_item["id"],
                conn_item["connector_type"],
                conn_item["name"],
                conn_item["description"],
                conn_item["category"],
                json.dumps(conn_item["operations"]),
                json.dumps(conn_item["permissions"]),
                conn_item["approval_required"],
                json.dumps(conn_item["allowed_bots"])
            ))
        conn.commit()


def _seed_default_schedules():
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM scheduled_tasks")
        if c.fetchone()["cnt"] > 0:
            return

        schedules = [
            {
                "id": "sched_reindex_knowledge",
                "name": "Knowledge Watch-Folders Sync",
                "description": "Periodically scans and indexes added or modified documents in registered watch folders.",
                "schedule_type": "interval",
                "interval_minutes": 120,
                "task_type": "reindex_library",
                "enabled": 1,
                "params": {"workspace_id": "ws_default"}
            },
            {
                "id": "sched_db_backup",
                "name": "Database Snapshot Backup",
                "description": "Creates a timestamped snapshot of chatbots.db in the backups directory.",
                "schedule_type": "daily",
                "daily_time": "03:00",
                "interval_minutes": 1440,
                "task_type": "backup_snapshot",
                "enabled": 1,
                "params": {}
            },
            {
                "id": "sched_repo_audit",
                "name": "Repository Health & Diff Digest",
                "description": "Checks local Git working tree for uncommitted modifications and repository status.",
                "schedule_type": "interval",
                "interval_minutes": 360,
                "task_type": "repo_audit",
                "enabled": 0,
                "params": {"repo_path": str(BASE)}
            }
        ]

        now = datetime.now()
        for s in schedules:
            next_run = now + timedelta(minutes=s["interval_minutes"])
            c.execute("""
                INSERT INTO scheduled_tasks (id, name, description, schedule_type, interval_minutes, daily_time, task_type, params_json, enabled, next_run_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["id"],
                s["name"],
                s["description"],
                s["schedule_type"],
                s["interval_minutes"],
                s.get("daily_time", "03:00"),
                s["task_type"],
                json.dumps(s["params"]),
                s["enabled"],
                next_run.strftime("%Y-%m-%d %H:%M:%S")
            ))
        conn.commit()


# ============================================================================
# WORKFLOW RECIPES CRUD & EXECUTION ENGINE
# ============================================================================

def list_recipes(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lists all available workflow recipes."""
    with _get_db() as conn:
        c = conn.cursor()
        if category:
            c.execute("SELECT * FROM workflow_recipes WHERE category = ? ORDER BY is_preset DESC, title ASC", (category,))
        else:
            c.execute("SELECT * FROM workflow_recipes ORDER BY is_preset DESC, title ASC")
        rows = c.fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "title": r["title"],
            "description": r["description"] or "",
            "icon": r["icon"] or "zap",
            "category": r["category"] or "general",
            "steps": json.loads(r["steps_json"] or "[]"),
            "input_schema": json.loads(r["input_schema_json"] or "[]"),
            "is_preset": bool(r["is_preset"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"]
        })
    return results


def get_recipe(recipe_id: str) -> Optional[Dict[str, Any]]:
    """Fetches a single recipe by ID."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM workflow_recipes WHERE id = ?", (recipe_id,))
        r = c.fetchone()
        if not r:
            return None
        return {
            "id": r["id"],
            "title": r["title"],
            "description": r["description"] or "",
            "icon": r["icon"] or "zap",
            "category": r["category"] or "general",
            "steps": json.loads(r["steps_json"] or "[]"),
            "input_schema": json.loads(r["input_schema_json"] or "[]"),
            "is_preset": bool(r["is_preset"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"]
        }


def save_recipe(
    recipe_id: Optional[str],
    title: str,
    description: str,
    category: str,
    steps: List[Dict[str, Any]],
    input_schema: List[Dict[str, Any]],
    icon: str = "zap"
) -> Dict[str, Any]:
    """Creates or updates a custom workflow recipe."""
    rec_id = recipe_id or f"recipe_{uuid.uuid4().hex[:10]}"
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO workflow_recipes (id, title, description, icon, category, steps_json, input_schema_json, is_preset, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                icon = excluded.icon,
                category = excluded.category,
                steps_json = excluded.steps_json,
                input_schema_json = excluded.input_schema_json,
                updated_at = CURRENT_TIMESTAMP
        """, (
            rec_id,
            title,
            description,
            icon,
            category,
            json.dumps(steps),
            json.dumps(input_schema)
        ))
        conn.commit()
    return get_recipe(rec_id)


def delete_recipe(recipe_id: str) -> bool:
    """Deletes a custom recipe (presets cannot be deleted)."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM workflow_recipes WHERE id = ? AND is_preset = 0", (recipe_id,))
        conn.commit()
        return c.rowcount > 0


# ============================================================================
# PERSISTENT BACKGROUND JOB QUEUE
# ============================================================================

def create_job(
    title: str,
    job_type: str,
    params: Dict[str, Any],
    recipe_id: Optional[str] = None,
    model_name: Optional[str] = None
) -> Dict[str, Any]:
    """Submits a new job to the persistent background queue."""
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO background_jobs (id, title, job_type, recipe_id, status, progress, progress_label, model_name, params_json, logs_json)
            VALUES (?, ?, ?, ?, 'queued', 0, 'Waiting in queue...', ?, ?, '[]')
        """, (
            job_id,
            title,
            job_type,
            recipe_id,
            model_name or "",
            json.dumps(params)
        ))
        conn.commit()
    return get_job(job_id)


def list_jobs(limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lists background jobs."""
    with _get_db() as conn:
        c = conn.cursor()
        if status:
            c.execute("SELECT * FROM background_jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            c.execute("SELECT * FROM background_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = c.fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "title": r["title"],
            "job_type": r["job_type"],
            "recipe_id": r["recipe_id"],
            "status": r["status"],
            "progress": r["progress"],
            "progress_label": r["progress_label"] or "",
            "model_name": r["model_name"] or "",
            "params": json.loads(r["params_json"] or "{}"),
            "result": json.loads(r["result_json"] or "{}"),
            "logs": json.loads(r["logs_json"] or "[]"),
            "error": r["error"],
            "cancel_requested": bool(r["cancel_requested"]),
            "created_at": r["created_at"],
            "started_at": r["started_at"],
            "completed_at": r["completed_at"]
        })
    return results


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Gets detailed info on a specific job."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,))
        r = c.fetchone()
        if not r:
            return None
        return {
            "id": r["id"],
            "title": r["title"],
            "job_type": r["job_type"],
            "recipe_id": r["recipe_id"],
            "status": r["status"],
            "progress": r["progress"],
            "progress_label": r["progress_label"] or "",
            "model_name": r["model_name"] or "",
            "params": json.loads(r["params_json"] or "{}"),
            "result": json.loads(r["result_json"] or "{}"),
            "logs": json.loads(r["logs_json"] or "[]"),
            "error": r["error"],
            "cancel_requested": bool(r["cancel_requested"]),
            "created_at": r["created_at"],
            "started_at": r["started_at"],
            "completed_at": r["completed_at"]
        }


def cancel_job(job_id: str) -> bool:
    """Requests cancellation of a background job."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE background_jobs
            SET cancel_requested = 1
            WHERE id = ? AND status IN ('queued', 'running')
        """, (job_id,))
        conn.commit()
        return c.rowcount > 0


def retry_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Resets a failed or cancelled job back to queued."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE background_jobs
            SET status = 'queued',
                progress = 0,
                progress_label = 'Requeued for execution',
                error = NULL,
                cancel_requested = 0,
                started_at = NULL,
                completed_at = NULL
            WHERE id = ?
        """, (job_id,))
        conn.commit()
    return get_job(job_id)


def delete_job(job_id: str) -> bool:
    """Deletes a job from history."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM background_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return c.rowcount > 0


def _append_job_log(job_id: str, level: str, message: str, progress: Optional[int] = None, label: Optional[str] = None):
    """Appends an entry to the job log array in SQLite."""
    now_str = datetime.now().strftime("%H:%M:%S")
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT logs_json, progress, progress_label FROM background_jobs WHERE id = ?", (job_id,))
        row = c.fetchone()
        if not row:
            return
        logs = json.loads(row["logs_json"] or "[]")
        logs.append({"time": now_str, "level": level, "msg": message})
        
        new_progress = progress if progress is not None else row["progress"]
        new_label = label if label is not None else row["progress_label"]

        c.execute("""
            UPDATE background_jobs
            SET logs_json = ?, progress = ?, progress_label = ?
            WHERE id = ?
        """, (json.dumps(logs), new_progress, new_label, job_id))
        conn.commit()


# ============================================================================
# JOB EXECUTION RUNNERS
# ============================================================================

def _check_cancellation(job_id: str):
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT cancel_requested FROM background_jobs WHERE id = ?", (job_id,))
        r = c.fetchone()
        if r and r["cancel_requested"]:
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user.")


def execute_job_sync(job_id: str) -> Dict[str, Any]:
    """Synchronous execution wrapper that can be called from background workers."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,))
        r = c.fetchone()
        if not r:
            return {"error": "Job not found"}

        job = dict(r)
        job_type = job["job_type"]
        params = json.loads(job["params_json"] or "{}")

        c.execute("""
            UPDATE background_jobs
            SET status = 'running', started_at = CURRENT_TIMESTAMP, progress = 5, progress_label = 'Executing...'
            WHERE id = ?
        """, (job_id,))
        conn.commit()

    _append_job_log(job_id, "INFO", f"Job '{job['title']}' started.")

    try:
        if job_type == "recipe":
            result = _run_recipe_job(job_id, job["recipe_id"], params)
        elif job_type == "reindex_library":
            result = _run_reindex_job(job_id, params)
        elif job_type == "repo_audit":
            result = _run_repo_audit_job(job_id, params)
        elif job_type == "backup_snapshot":
            result = _run_backup_job(job_id, params)
        elif job_type == "custom":
            result = _run_custom_job(job_id, params)
        else:
            raise ValueError(f"Unknown job type: {job_type}")

        with _get_db() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE background_jobs
                SET status = 'completed', progress = 100, progress_label = 'Completed successfully',
                    result_json = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (json.dumps(result), job_id))
            conn.commit()

        _append_job_log(job_id, "SUCCESS", "Job finished successfully.", progress=100, label="Completed")
        return {"ok": True, "result": result}

    except asyncio.CancelledError:
        with _get_db() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE background_jobs
                SET status = 'cancelled', progress_label = 'Cancelled by user',
                    completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (job_id,))
            conn.commit()
        _append_job_log(job_id, "WARN", "Job execution was cancelled.")
        return {"ok": False, "cancelled": True}

    except Exception as e:
        err_msg = str(e)
        logger.exception(f"Job {job_id} failed: {err_msg}")
        with _get_db() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE background_jobs
                SET status = 'failed', error = ?, progress_label = 'Failed',
                    completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (err_msg, job_id))
            conn.commit()
        _append_job_log(job_id, "ERROR", f"Job execution error: {err_msg}")
        return {"ok": False, "error": err_msg}


def _run_recipe_job(job_id: str, recipe_id: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Executes a multi-step recipe pipeline sequentially."""
    recipe = get_recipe(recipe_id)
    if not recipe:
        raise ValueError(f"Recipe '{recipe_id}' not found.")

    steps = recipe["steps"]
    total_steps = len(steps)
    step_outputs = {}

    _append_job_log(job_id, "INFO", f"Running recipe '{recipe['title']}' with {total_steps} steps.")

    for idx, step in enumerate(steps, start=1):
        _check_cancellation(job_id)

        step_id = step["id"]
        step_name = step.get("name", f"Step {idx}")
        step_type = step.get("type", "llm_generate")
        raw_prompt = step.get("prompt", "")

        progress_pct = int(10 + ((idx - 1) / total_steps) * 80)
        _append_job_log(job_id, "INFO", f"Starting Step {idx}/{total_steps}: {step_name}", progress=progress_pct, label=f"Step {idx}/{total_steps}: {step_name}")

        # Interpolate variables: {{inputs.key}} and {{steps.step_id.output}}
        prompt = raw_prompt
        for k, v in inputs.items():
            prompt = prompt.replace(f"{{{{inputs.{k}}}}}", str(v))
        for prev_id, out in step_outputs.items():
            prompt = prompt.replace(f"{{{{steps.{prev_id}.output}}}}", str(out))

        output_text = ""

        if step_type == "git_diff_inspect":
            # Direct git status inspection
            try:
                import dev_workbench
                repo_path = Path(inputs.get("repo_path") or str(BASE))
                status_res = dev_workbench.get_git_status(repo_path)
                diff_summary = f"Branch: {status_res.get('branch', 'unknown')}\nClean: {status_res.get('is_clean')}\nChanged Files:\n"
                for f in status_res.get("files", [])[:15]:
                    diff_summary += f" - {f.get('status')} {f.get('path')}\n"
                output_text = diff_summary
            except Exception as e:
                output_text = f"Git inspection failed: {str(e)}"

        elif step_type == "llm_generate":
            # Invoke local Ollama
            model = inputs.get("model") or "Qwen3.5-uncensored:latest"
            try:
                res = requests.post(
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "You are Lamplight's autonomous workflow execution engine. Provide thorough, well-structured, authoritative answers."},
                            {"role": "user", "content": prompt}
                        ],
                        "options": {"temperature": 0.4, "top_p": 0.9, "num_predict": 2048},
                        "stream": False
                    },
                    timeout=90
                )
                if res.status_code == 200:
                    msg_obj = res.json().get("message", {})
                    output_text = msg_obj.get("content", "").strip()
                    if not output_text and msg_obj.get("thinking"):
                        output_text = msg_obj.get("thinking", "").strip()
                else:
                    output_text = f"LLM error: HTTP {res.status_code} - {res.text[:200]}"
            except Exception as e:
                output_text = f"LLM invocation failed: {str(e)}"

        step_outputs[step_id] = output_text
        _append_job_log(job_id, "INFO", f"Completed Step {idx}: {step_name} ({len(output_text)} chars generated)")

    return {
        "recipe_id": recipe_id,
        "title": recipe["title"],
        "inputs": inputs,
        "step_outputs": step_outputs,
        "final_output": list(step_outputs.values())[-1] if step_outputs else ""
    }


def _run_reindex_job(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Runs a watch-folder reindexing job."""
    _append_job_log(job_id, "INFO", "Reindexing all registered watch folders...")
    try:
        import knowledge_library
        ws_id = params.get("workspace_id", "ws_default")
        sync_results = knowledge_library.sync_all_watch_folders(ws_id)
        _append_job_log(job_id, "INFO", f"Synchronized {len(sync_results)} watch folder(s).", progress=90)
        return {"folders_synced": len(sync_results), "details": sync_results}
    except Exception as e:
        raise RuntimeError(f"Reindex failed: {str(e)}")


def _run_repo_audit_job(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Runs a Git repository health audit."""
    _append_job_log(job_id, "INFO", "Inspecting Git repository status and branch health...")
    try:
        import dev_workbench
        repo_path = Path(params.get("repo_path") or str(BASE))
        status_res = dev_workbench.get_git_status(repo_path)
        _append_job_log(job_id, "INFO", f"Repo on branch '{status_res.get('branch')}', {len(status_res.get('files', []))} changed files.", progress=90)
        return status_res
    except Exception as e:
        raise RuntimeError(f"Repo audit failed: {str(e)}")


def _run_backup_job(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Creates a snapshot copy of chatbots.db in the backup folder."""
    _append_job_log(job_id, "INFO", "Preparing database backup snapshot...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_file = BACKUP_DIR / f"chatbots_backup_{ts}.db"
    
    shutil.copy2(DB_PATH, target_file)
    size_mb = round(target_file.stat().st_size / (1024 * 1024), 2)
    
    _append_job_log(job_id, "SUCCESS", f"Backup created: {target_file.name} ({size_mb} MB)", progress=90)
    return {
        "filename": target_file.name,
        "size_mb": size_mb,
        "backup_path": str(target_file)
    }


def _run_custom_job(job_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Runs custom lightweight batch tasks."""
    task_name = params.get("name", "Custom Batch Task")
    _append_job_log(job_id, "INFO", f"Processing custom batch: {task_name}")
    time.sleep(1)
    return {"message": f"Processed {task_name}", "timestamp": datetime.now().isoformat()}


# ============================================================================
# SCHEDULED TASKS ENGINE
# ============================================================================

def list_scheduled_tasks() -> List[Dict[str, Any]]:
    """Lists all configured scheduled tasks."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM scheduled_tasks ORDER BY created_at ASC")
        rows = c.fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "name": r["name"],
            "description": r["description"] or "",
            "schedule_type": r["schedule_type"],
            "interval_minutes": r["interval_minutes"],
            "daily_time": r["daily_time"] or "09:00",
            "task_type": r["task_type"],
            "recipe_id": r["recipe_id"],
            "params": json.loads(r["params_json"] or "{}"),
            "enabled": bool(r["enabled"]),
            "last_run_at": r["last_run_at"],
            "next_run_at": r["next_run_at"],
            "last_status": r["last_status"] or "idle",
            "last_error": r["last_error"],
            "run_count": r["run_count"],
            "created_at": r["created_at"]
        })
    return results


def create_or_update_scheduled_task(
    task_id: Optional[str],
    name: str,
    description: str,
    schedule_type: str,
    interval_minutes: int,
    daily_time: str,
    task_type: str,
    recipe_id: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    enabled: bool = True
) -> Dict[str, Any]:
    """Creates or updates a scheduled recurring task."""
    tid = task_id or f"sched_{uuid.uuid4().hex[:10]}"
    params_dict = params or {}
    now = datetime.now()
    next_run = now + timedelta(minutes=interval_minutes)

    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO scheduled_tasks (id, name, description, schedule_type, interval_minutes, daily_time, task_type, recipe_id, params_json, enabled, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                schedule_type = excluded.schedule_type,
                interval_minutes = excluded.interval_minutes,
                daily_time = excluded.daily_time,
                task_type = excluded.task_type,
                recipe_id = excluded.recipe_id,
                params_json = excluded.params_json,
                enabled = excluded.enabled,
                next_run_at = excluded.next_run_at
        """, (
            tid,
            name,
            description,
            schedule_type,
            interval_minutes,
            daily_time,
            task_type,
            recipe_id,
            json.dumps(params_dict),
            1 if enabled else 0,
            next_run.strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()

    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (tid,))
        row = c.fetchone()
        return dict(row)


def toggle_scheduled_task(task_id: str, enabled: bool) -> bool:
    """Enables or pauses a scheduled task."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE scheduled_tasks SET enabled = ? WHERE id = ?", (1 if enabled else 0, task_id))
        conn.commit()
        return c.rowcount > 0


def delete_scheduled_task(task_id: str) -> bool:
    """Deletes a scheduled task."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
        return c.rowcount > 0


def trigger_scheduled_task_now(task_id: str) -> Dict[str, Any]:
    """Manually triggers a scheduled task immediately via the background queue."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        task = c.fetchone()
        if not task:
            raise ValueError(f"Task '{task_id}' not found.")

        t = dict(task)
        params = json.loads(t["params_json"] or "{}")
        job = create_job(
            title=f"[Scheduled Run] {t['name']}",
            job_type=t["task_type"],
            params=params,
            recipe_id=t["recipe_id"]
        )

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        next_run = (datetime.now() + timedelta(minutes=t["interval_minutes"])).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("""
            UPDATE scheduled_tasks
            SET last_run_at = ?, next_run_at = ?, run_count = run_count + 1, last_status = 'triggered'
            WHERE id = ?
        """, (now_str, next_run, task_id))
        conn.commit()

    return {"ok": True, "job_id": job["id"], "job": job}


def check_and_dispatch_due_schedules():
    """Polls scheduled tasks and enqueues background jobs for any that are due."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    due_tasks = []
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM scheduled_tasks
            WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)
        """, (now_str,))
        due_tasks = [dict(r) for r in c.fetchall()]

    for task in due_tasks:
        try:
            trigger_scheduled_task_now(task["id"])
            logger.info(f"Dispatched scheduled task: {task['name']}")
        except Exception as e:
            logger.error(f"Failed to trigger scheduled task {task['id']}: {str(e)}")


# ============================================================================
# TOOL & CONNECTOR REGISTRY
# ============================================================================

def list_connectors() -> List[Dict[str, Any]]:
    """Lists all registered tools and external connectors."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM tool_connectors ORDER BY category ASC, name ASC")
        rows = c.fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "connector_type": r["connector_type"],
            "name": r["name"],
            "description": r["description"],
            "category": r["category"],
            "operations": json.loads(r["operations_json"] or "[]"),
            "permissions": json.loads(r["permissions_json"] or "[]"),
            "approval_required": bool(r["approval_required"]),
            "allowed_bots": json.loads(r["allowed_bots_json"] or '["*"]'),
            "enabled": bool(r["enabled"]),
            "config": json.loads(r["config_json"] or "{}"),
            "updated_at": r["updated_at"]
        })
    return results


def update_connector_settings(
    connector_id: str,
    enabled: bool,
    approval_required: bool,
    allowed_bots: List[str]
) -> bool:
    """Updates permission and approval settings for a tool connector."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE tool_connectors
            SET enabled = ?, approval_required = ?, allowed_bots_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            1 if enabled else 0,
            1 if approval_required else 0,
            json.dumps(allowed_bots),
            connector_id
        ))
        conn.commit()
        return c.rowcount > 0


def verify_tool_access(connector_type: str, bot_name: str) -> Dict[str, Any]:
    """Audits whether a given bot has permission to use a connector."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM tool_connectors WHERE connector_type = ?", (connector_type,))
        r = c.fetchone()
        if not r:
            return {"allowed": False, "reason": f"Tool '{connector_type}' is not registered in connector registry."}

        if not r["enabled"]:
            return {"allowed": False, "reason": f"Tool '{r['name']}' is currently disabled in the Connector Registry."}

        allowed_bots = json.loads(r["allowed_bots_json"] or '["*"]')
        if "*" not in allowed_bots and bot_name.lower() not in [b.lower() for b in allowed_bots]:
            return {
                "allowed": False,
                "reason": f"Bot '{bot_name}' is not authorized to access '{r['name']}'. Permitted bots: {allowed_bots}"
            }

        return {
            "allowed": True,
            "requires_approval": bool(r["approval_required"]),
            "connector_name": r["name"],
            "permissions": json.loads(r["permissions_json"] or "[]")
        }


# ============================================================================
# BACKGROUND WORKER & SCHEDULER THREADS
# ============================================================================

_worker_thread = None
_sched_thread = None
_stop_workers = False

def _background_worker_loop():
    global _stop_workers
    while not _stop_workers:
        try:
            with _get_db() as conn:
                c = conn.cursor()
                c.execute("SELECT id FROM background_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1")
                row = c.fetchone()
                job_id = row["id"] if row else None

            if job_id:
                execute_job_sync(job_id)
            else:
                time.sleep(2)
        except Exception as e:
            logger.error(f"Error in background job worker: {e}")
            time.sleep(3)

def _scheduler_worker_loop():
    global _stop_workers
    while not _stop_workers:
        try:
            check_and_dispatch_due_schedules()
        except Exception as e:
            logger.error(f"Error in scheduler worker: {e}")
        time.sleep(30)

def start_background_workers():
    global _worker_thread, _sched_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_background_worker_loop, daemon=True, name="LamplightJobWorker")
        _worker_thread.start()
    if _sched_thread is None or not _sched_thread.is_alive():
        _sched_thread = threading.Thread(target=_scheduler_worker_loop, daemon=True, name="LamplightScheduler")
        _sched_thread.start()

# Automatically initialize tables and start worker daemons
init_automation_tables()
start_background_workers()
