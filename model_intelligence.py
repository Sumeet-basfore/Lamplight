"""
Model Intelligence, Router, Context Engineering & Prompt Debugger Engine for Lamplight.
Provides:
1. Detailed Model Capability Profiles (context length, parameters, thinking, tools, vision, quantization).
2. Task-Based Model Router (detects task category and routes to best available local model).
3. Resource-Aware Scheduler & Headroom Guard (inspects VRAM/RAM, active models, avoids OOM).
4. Reusable Generation Presets (Coding, Research, Creative, Fast Q&A, Strict JSON).
5. Structured Output Mode (JSON schema formatting, validation, and auto-repair).
6. Answer Verification Mode (Dual-model secondary critic / hallucination checker).
7. Request Recorder & Prompt Debugger (stores exact payload, token breakdown, parameters, diffs).
"""

import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
import requests

DB_PATH = os.path.expanduser("~/ChatBots/chatbots.db")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# Default Built-In Generation Presets
BUILTIN_PRESETS = [
    {
        "id": "preset_balanced",
        "name": "General Balanced",
        "description": "Standard balanced temperature for everyday conversational answers and assistance.",
        "category": "general",
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "num_predict": 4096,
        "thinking_level": "balanced",
        "is_builtin": 1
    },
    {
        "id": "preset_coding",
        "name": "Coding & Architecture",
        "description": "Deterministic, precise code generation with deep syntax rigor, minimal hallucination.",
        "category": "coding",
        "temperature": 0.2,
        "top_p": 0.95,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.1,
        "num_predict": 8192,
        "thinking_level": "deep",
        "is_builtin": 1
    },
    {
        "id": "preset_research",
        "name": "Deep Research & Synthesis",
        "description": "Structured analytical reasoning with source citations, logical scrutiny, and evidence verification.",
        "category": "research",
        "temperature": 0.35,
        "top_p": 0.9,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "num_predict": 8192,
        "thinking_level": "deep",
        "is_builtin": 1
    },
    {
        "id": "preset_creative",
        "name": "Creative Writing & Muse",
        "description": "High creative variance, evocative metaphors, vibrant character voices, and sensory prose.",
        "category": "creative",
        "temperature": 0.88,
        "top_p": 0.95,
        "presence_penalty": 0.3,
        "frequency_penalty": 0.2,
        "num_predict": 6144,
        "thinking_level": "low",
        "is_builtin": 1
    },
    {
        "id": "preset_fast",
        "name": "Fast Q&A & Definitions",
        "description": "Ultra-low latency direct replies without thinking delay, optimized for fast lookups.",
        "category": "fast_qa",
        "temperature": 0.4,
        "top_p": 0.9,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "num_predict": 2048,
        "thinking_level": "off",
        "is_builtin": 1
    },
    {
        "id": "preset_structured",
        "name": "Strict JSON / Structured",
        "description": "Near-zero temperature for deterministic JSON schema compliance and data extraction.",
        "category": "structured",
        "temperature": 0.1,
        "top_p": 0.8,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "num_predict": 4096,
        "thinking_level": "off",
        "is_builtin": 1
    }
]

# Standard JSON Schemas for Structured Output
STANDARD_SCHEMAS = {
    "code_review": {
        "title": "Code Review & Security Audit",
        "schema": {
            "summary": "string - high-level overview of review",
            "score": "integer 1-100",
            "findings": [
                {
                    "severity": "critical | high | medium | low | info",
                    "title": "string",
                    "file_line": "string or null",
                    "description": "string",
                    "suggested_fix": "string"
                }
            ],
            "architecture_notes": "string",
            "approval_recommendation": "approve | request_changes | comment"
        }
    },
    "entity_extraction": {
        "title": "Entity & Concept Extraction",
        "schema": {
            "source_topic": "string",
            "key_entities": [
                {
                    "name": "string",
                    "category": "person | place | org | concept | technology | event",
                    "description": "string",
                    "relationships": ["string"]
                }
            ],
            "core_theses": ["string"],
            "open_questions": ["string"]
        }
    },
    "action_plan": {
        "title": "Step-by-Step Action Plan",
        "schema": {
            "objective": "string",
            "estimated_effort": "string",
            "phases": [
                {
                    "phase_number": "integer",
                    "title": "string",
                    "tasks": [
                        {
                            "task_id": "string",
                            "description": "string",
                            "priority": "high | medium | low",
                            "verification_criteria": "string"
                        }
                    ]
                }
            ],
            "risks_and_mitigations": [
                {
                    "risk": "string",
                    "mitigation": "string"
                }
            ]
        }
    },
    "summary_card": {
        "title": "Executive Summary Card",
        "schema": {
            "title": "string",
            "one_line_takeaway": "string",
            "key_takeaways": ["string"],
            "pros": ["string"],
            "cons": ["string"],
            "final_verdict": "string"
        }
    }
}


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_model_intelligence_tables():
    """Initializes tables for model profiles, generation presets, request records, and fallbacks."""
    with _get_db() as conn:
        c = conn.cursor()
        
        # 1. Model Capability Profiles Cache
        c.execute("""
            CREATE TABLE IF NOT EXISTS model_capability_profiles (
                model_name TEXT PRIMARY KEY,
                provider_type TEXT DEFAULT 'ollama',
                parameter_size TEXT DEFAULT '',
                quantization TEXT DEFAULT '',
                family TEXT DEFAULT '',
                context_length INTEGER DEFAULT 8192,
                supports_thinking INTEGER DEFAULT 0,
                thinking_levels TEXT DEFAULT '["off"]',
                supports_tools INTEGER DEFAULT 0,
                supports_vision INTEGER DEFAULT 0,
                supports_json INTEGER DEFAULT 1,
                recommended_tasks TEXT DEFAULT '["general"]',
                vram_estimate_mb INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Reusable Generation Presets
        c.execute("""
            CREATE TABLE IF NOT EXISTS generation_presets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT 'general',
                temperature REAL DEFAULT 0.7,
                top_p REAL DEFAULT 0.9,
                presence_penalty REAL DEFAULT 0.0,
                frequency_penalty REAL DEFAULT 0.0,
                num_predict INTEGER DEFAULT 4096,
                thinking_level TEXT DEFAULT 'balanced',
                system_prefix TEXT DEFAULT '',
                is_builtin INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Seed builtin presets if missing
        count = c.execute("SELECT COUNT(*) as n FROM generation_presets").fetchone()["n"]
        if count == 0:
            for p in BUILTIN_PRESETS:
                c.execute("""
                    INSERT OR REPLACE INTO generation_presets
                    (id, name, description, category, temperature, top_p, presence_penalty,
                     frequency_penalty, num_predict, thinking_level, is_builtin)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    p["id"], p["name"], p["description"], p["category"],
                    p["temperature"], p["top_p"], p["presence_penalty"],
                    p["frequency_penalty"], p["num_predict"], p["thinking_level"],
                    p["is_builtin"]
                ))

        # 3. Request Recorder & Prompt Debugger Logs
        c.execute("""
            CREATE TABLE IF NOT EXISTS request_records (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                workspace_id TEXT,
                mode TEXT DEFAULT 'friend',
                requested_model TEXT NOT NULL,
                effective_model TEXT NOT NULL,
                task_type TEXT DEFAULT 'general',
                routing_rationale TEXT DEFAULT '',
                preset_id TEXT,
                messages_json TEXT NOT NULL,
                options_json TEXT NOT NULL,
                system_prompt_chars INTEGER DEFAULT 0,
                context_tokens_est INTEGER DEFAULT 0,
                memories_injected_count INTEGER DEFAULT 0,
                fts_docs_injected_count INTEGER DEFAULT 0,
                response_preview TEXT DEFAULT '',
                response_full TEXT DEFAULT '',
                thinking_full TEXT DEFAULT '',
                duration_ms INTEGER DEFAULT 0,
                tokens_per_sec REAL DEFAULT 0.0,
                status TEXT DEFAULT 'ok',
                fallback_used INTEGER DEFAULT 0,
                structured_schema TEXT,
                structured_valid INTEGER DEFAULT 1,
                verification_critique TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_req_rec_created ON request_records(created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_req_rec_session ON request_records(session_id)")

        # 4. Fallback Configurations
        c.execute("""
            CREATE TABLE IF NOT EXISTS model_fallbacks (
                primary_model TEXT PRIMARY KEY,
                fallback_models TEXT NOT NULL, -- JSON array of fallback model names
                is_enabled INTEGER DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


# Call init on import
init_model_intelligence_tables()


# ============================================================================
# 1. Model Capability Profile Scanner
# ============================================================================

def scan_model_capabilities(model_name: str, provider_type: str = "ollama", base_url: str = OLLAMA_URL) -> Dict[str, Any]:
    """Inspects a model via Ollama /api/show or heuristics and generates a rich capability profile."""
    clean_name = model_name.split("::")[0].strip()
    if not clean_name:
        return {}

    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM model_capability_profiles WHERE model_name = ?", (clean_name,)).fetchone()
        if row and (time.time() - time.mktime(time.strptime(row["updated_at"][:19], "%Y-%m-%d %H:%M:%S")) < 3600):
            d = dict(row)
            d["thinking_levels"] = json.loads(d["thinking_levels"]) if isinstance(d["thinking_levels"], str) else d["thinking_levels"]
            d["recommended_tasks"] = json.loads(d["recommended_tasks"]) if isinstance(d["recommended_tasks"], str) else d["recommended_tasks"]
            return d

    # Fetch from Ollama
    supports_thinking = False
    thinking_levels = ["off"]
    supports_tools = False
    supports_vision = False
    supports_json = True
    context_length = 8192
    family = "unknown"
    param_size = "unknown"
    quantization = "unknown"
    vram_est = 0
    recommended_tasks = ["general"]

    m_lower = clean_name.lower()

    if provider_type == "ollama":
        try:
            r = requests.post(f"{base_url}/api/show", json={"name": clean_name}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                details = data.get("details", {})
                family = details.get("family", "unknown")
                param_size = details.get("parameter_size", "unknown")
                quantization = details.get("quantization_level", "unknown")
                caps = [str(x).lower() for x in data.get("capabilities", [])]
                tmpl = str(data.get("template", ""))
                model_info = data.get("model_info", {})

                # Find context length from model_info
                for k, v in model_info.items():
                    if "context_length" in k:
                        try:
                            context_length = int(v)
                        except Exception:
                            pass

                # Vision detection
                if "vision" in caps or any(x in m_lower for x in ["vision", "llava", "bakllava", "moondream", "minicpm-v"]):
                    supports_vision = True

                # Thinking detection
                has_cap = "thinking" in caps
                has_tmpl = ("<think>" in tmpl) or ("reasoning_content" in tmpl) or ("[think]" in tmpl)
                known_reasoning = any(x in m_lower or x in family.lower() for x in ["deepseek-r1", "qwq", "qwen3", "r1"])
                if has_cap or has_tmpl or known_reasoning:
                    supports_thinking = True
                    thinking_levels = ["off", "low", "balanced", "deep"]

                # Tools detection
                if "tools" in caps or "tool_calls" in tmpl or any(x in m_lower for x in ["llama3.1", "llama3.2", "llama3.3", "qwen2.5", "mistral", "hermes"]):
                    supports_tools = True

        except Exception:
            pass

    # Heuristics based on name if show failed or values remain default
    if any(x in m_lower for x in ["coder", "codellama", "qwen2.5-coder", "starcoder", "deepseek-coder"]):
        recommended_tasks.append("coding")
    if any(x in m_lower for x in ["r1", "qwq", "reasoning", "command-r", "deepseek"]):
        recommended_tasks.append("research")
    if any(x in m_lower for x in ["creative", "fiction", "story", "mythomax", "solar", "muse"]):
        recommended_tasks.append("creative")
    if any(x in m_lower for x in ["mini", "0.5b", "1.5b", "1b", "3b", "tiny"]):
        recommended_tasks.append("fast_qa")
    if supports_vision:
        recommended_tasks.append("vision")
    recommended_tasks = list(dict.fromkeys(recommended_tasks))

    # Calculate rough VRAM estimate based on parameter size
    try:
        if "b" in param_size.lower():
            num = float(re.sub(r"[^0-9.]", "", param_size))
            vram_est = int(num * 0.75 * 1024) # approx 4-bit quant
    except Exception:
        vram_est = 4096

    profile = {
        "model_name": clean_name,
        "provider_type": provider_type,
        "parameter_size": param_size,
        "quantization": quantization,
        "family": family,
        "context_length": context_length,
        "supports_thinking": 1 if supports_thinking else 0,
        "thinking_levels": thinking_levels,
        "supports_tools": 1 if supports_tools else 0,
        "supports_vision": 1 if supports_vision else 0,
        "supports_json": 1 if supports_json else 0,
        "recommended_tasks": recommended_tasks,
        "vram_estimate_mb": vram_est,
        "notes": f"Detected {param_size} {family} model"
    }

    # Save to database
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO model_capability_profiles
            (model_name, provider_type, parameter_size, quantization, family,
             context_length, supports_thinking, thinking_levels, supports_tools,
             supports_vision, supports_json, recommended_tasks, vram_estimate_mb, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            profile["model_name"], profile["provider_type"], profile["parameter_size"],
            profile["quantization"], profile["family"], profile["context_length"],
            profile["supports_thinking"], json.dumps(profile["thinking_levels"]),
            profile["supports_tools"], profile["supports_vision"], profile["supports_json"],
            json.dumps(profile["recommended_tasks"]), profile["vram_estimate_mb"], profile["notes"]
        ))
        conn.commit()

    return profile


def get_all_model_profiles(available_model_names: List[str]) -> List[Dict[str, Any]]:
    """Returns profiles for all given model names, scanning any missing."""
    profiles = []
    for m in available_model_names:
        prof = scan_model_capabilities(m)
        if prof:
            profiles.append(prof)
    return profiles


# ============================================================================
# 2. Task-Based Model Router
# ============================================================================

def detect_task_type(prompt_text: str, attachments: Optional[List[Dict[str, Any]]] = None, explicit_task: Optional[str] = None) -> Tuple[str, float]:
    """
    Analyzes prompt and attachments to determine task category:
    - 'vision': Image attached or visual analysis requested.
    - 'coding': Code snippets, syntax keywords, refactoring, bugs, git.
    - 'research': In-depth explanation, citations, comparison, methodology, evidence.
    - 'creative': Narrative storytelling, poetry, character dialogue, worldbuilding.
    - 'structured': JSON extraction, schema requests, tabular formatting.
    - 'fast_qa': Short factual queries (< 60 chars, direct questions).
    - 'general': Everyday chat.
    Returns (task_type, confidence_score 0.0 - 1.0).
    """
    if explicit_task and explicit_task in ["coding", "research", "creative", "vision", "structured", "fast_qa", "general"]:
        return explicit_task, 1.0

    # 1. Vision Check
    if attachments:
        for att in attachments:
            m = (att.get("mime") or att.get("type") or "").lower()
            fn = (att.get("name") or att.get("filename") or "").lower()
            if any(x in m for x in ["image", "png", "jpg", "jpeg", "webp"]) or any(fn.endswith(x) for x in [".png", ".jpg", ".jpeg", ".webp"]):
                return "vision", 0.99

    text = (prompt_text or "").strip()
    if not text:
        return "general", 0.5

    t_lower = text.lower()

    # 2. Fast Q&A Check
    if len(text) < 70 and ("?" in text or any(t_lower.startswith(w) for w in ["what is", "who is", "when did", "define ", "capital of", "calculate", "how many"])):
        if not any(k in t_lower for k in ["code", "function", "write a story", "detailed report"]):
            return "fast_qa", 0.85

    # 3. Coding Keywords
    coding_signals = [
        "```", "def ", "function", "class ", "import ", "const ", "var ", "let ",
        "return ", "exception", "refactor", "bug", "traceback", "git ", "sqlite",
        "sql ", "query", "endpoint", "api", "regex", "docker", "test case", "pull request",
        "compile", "syntax error", "nullpointer", "algorithm", "asyncio"
    ]
    coding_score = sum(1 for sig in coding_signals if sig in t_lower)
    if "```" in text or coding_score >= 2 or any(t_lower.startswith(x) for x in ["write a python", "write a script", "write code", "fix this code"]):
        return "coding", min(0.95, 0.6 + coding_score * 0.1)

    # 4. Structured Output Signals
    if any(x in t_lower for x in ["json schema", "output in json", "as a json", "json format", "extract entities", "table format", "csv format"]):
        return "structured", 0.92

    # 5. Creative Writing Signals
    creative_signals = [
        "story", "narrative", "chapter", "novel", "character", "protagonist",
        "antagonist", "dialogue", "poem", "poetry", "stanza", "rhyme", "fantasy",
        "sci-fi", "plot twist", "prose", "lore", "creative writing", "muse", "fiction",
        "worldbuilding", "mythology", "wizard", "magic system", "tale"
    ]
    creative_score = sum(1 for sig in creative_signals if sig in t_lower)
    if creative_score >= 2 or any(x in t_lower for x in ["write a story", "write a poem", "continue the scene", "short story", "fantasy story", "character arc"]):
        return "creative", min(0.95, 0.65 + creative_score * 0.1)

    # 6. Deep Research Signals
    research_signals = [
        "explain in depth", "comprehensive analysis", "compare and contrast",
        "peer-reviewed", "citations", "literature", "synthesis", "methodology",
        "pros and cons", "evaluate the arguments", "historical context", "empirical",
        "investigate", "break down the concepts", "scientific basis"
    ]
    research_score = sum(1 for sig in research_signals if sig in t_lower)
    if research_score >= 2 or len(text) > 400:
        return "research", min(0.9, 0.6 + research_score * 0.1)

    return "general", 0.6


def route_model(task_type: str, requested_model: Optional[str], available_models: List[str]) -> Tuple[str, Dict[str, Any]]:
    """
    Selects the best available model for the given task.
    If requested_model is provided and != 'auto', respects user choice.
    Otherwise picks the optimal model according to capability profiles.
    Returns (effective_model, routing_meta).
    """
    if requested_model and requested_model != "auto":
        return requested_model, {
            "routed": False,
            "mode": "manual_override",
            "task_type": task_type,
            "model": requested_model,
            "rationale": f"User manually pinned model '{requested_model}'."
        }

    if not available_models:
        return requested_model or "qwen2.5:latest", {
            "routed": False,
            "mode": "fallback_default",
            "task_type": task_type,
            "model": requested_model or "qwen2.5:latest",
            "rationale": "No candidate models available in registry."
        }

    # Fetch profiles for available models
    profiles = get_all_model_profiles(available_models)
    
    # Priority scoring map
    best_model = available_models[0]
    best_score = -1.0
    best_rationale = "Default fallback"

    for prof in profiles:
        m_name = prof["model_name"]
        tasks = prof.get("recommended_tasks", [])
        m_lower = m_name.lower()
        score = 0.0

        if task_type == "coding":
            if "coding" in tasks:
                score += 10.0
            if "coder" in m_lower or "deepseek-coder" in m_lower or "qwen2.5-coder" in m_lower:
                score += 15.0
            if prof.get("supports_tools"):
                score += 3.0

        elif task_type == "research":
            if "research" in tasks:
                score += 10.0
            if prof.get("supports_thinking"):
                score += 12.0
            if "r1" in m_lower or "qwq" in m_lower:
                score += 15.0

        elif task_type == "creative":
            if "creative" in tasks:
                score += 12.0
            if any(x in m_lower for x in ["solar", "mythomax", "mistral", "llama"]):
                score += 5.0

        elif task_type == "vision":
            if prof.get("supports_vision") or any(x in m_lower for x in ["vision", "llava", "minicpm"]):
                score += 30.0

        elif task_type == "structured":
            if prof.get("supports_tools") or prof.get("supports_json"):
                score += 8.0
            if "qwen2.5" in m_lower or "llama3" in m_lower:
                score += 6.0

        elif task_type == "fast_qa":
            if "fast_qa" in tasks:
                score += 10.0
            if any(x in m_lower for x in ["1.5b", "3b", "1b", "0.5b", "mini"]):
                score += 15.0

        else: # general
            score += 5.0
            if "qwen" in m_lower or "llama" in m_lower:
                score += 5.0

        if score > best_score:
            best_score = score
            best_model = m_name
            best_rationale = f"Matched capabilities for {task_type.upper()} (score: {score:.1f})"

    return best_model, {
        "routed": True,
        "mode": "auto_task_router",
        "task_type": task_type,
        "model": best_model,
        "score": best_score,
        "rationale": best_rationale
    }


# ============================================================================
# 3. Resource-Aware Scheduler & Headroom Guard
# ============================================================================

def get_system_resource_headroom() -> Dict[str, Any]:
    """Checks live VRAM and RAM headroom to ensure safe model loading."""
    headroom = {
        "gpu_available": False,
        "vram_total_mb": 0,
        "vram_used_mb": 0,
        "vram_free_mb": 0,
        "vram_percent": 0.0,
        "ram_total_mb": 0,
        "ram_free_mb": 0,
        "loaded_models": [],
        "safe_to_load_large": True,
        "warning": None
    }

    # 1. Inspect GPU via nvidia-smi
    try:
        res = subprocess.run([
            "nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free",
            "--format=csv,nounits,noheader"
        ], capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            tot, used, free = [float(x.strip()) for x in res.stdout.strip().split(",")]
            headroom["gpu_available"] = True
            headroom["vram_total_mb"] = int(tot)
            headroom["vram_used_mb"] = int(used)
            headroom["vram_free_mb"] = int(free)
            headroom["vram_percent"] = round((used / tot) * 100, 1) if tot > 0 else 0
            if headroom["vram_percent"] > 88:
                headroom["safe_to_load_large"] = False
                headroom["warning"] = f"VRAM critically high ({headroom['vram_percent']}% used). Large model load may cause swapping."
    except Exception:
        pass

    # 2. Inspect Loaded Models via Ollama /api/ps
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=2)
        if r.status_code == 200:
            models = r.json().get("models", [])
            headroom["loaded_models"] = [
                {
                    "name": m.get("name"),
                    "size_vram_mb": int(m.get("size_vram", 0) / (1024 * 1024)),
                    "expires_at": m.get("expires_at")
                }
                for m in models
            ]
    except Exception:
        pass

    return headroom


def eject_idle_models(keep_model: Optional[str] = None) -> List[str]:
    """Evicts idle models from VRAM to make room for new inference."""
    evicted = []
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=2)
        if r.status_code == 200:
            for m in r.json().get("models", []):
                name = m.get("name")
                if name and name != keep_model:
                    # Setting keep_alive: 0 immediately unloads it
                    requests.post(f"{OLLAMA_URL}/api/generate", json={"model": name, "keep_alive": 0}, timeout=2)
                    evicted.append(name)
    except Exception:
        pass
    return evicted


# ============================================================================
# 4. Structured Output Mode (JSON Schema Validation & Repair)
# ============================================================================

def build_structured_prompt_instructions(schema: Any, schema_name: Optional[str] = None) -> str:
    """Creates clear JSON schema system instructions."""
    schema_str = json.dumps(schema, indent=2) if isinstance(schema, (dict, list)) else str(schema)
    name_label = f" ({schema_name})" if schema_name else ""
    return (
        f"\n\n### [MANDATORY STRUCTURED OUTPUT FORMAT{name_label}]\n"
        "You must output ONLY valid, parseable JSON conforming exactly to the schema below. "
        "Do not include any conversational preamble, commentary, markdown explanation, or trailing text. "
        "Begin your output with '{' and end with '}':\n"
        f"```json\n{schema_str}\n```\n"
    )


def validate_structured_response(response_text: str, schema: Optional[Any] = None) -> Dict[str, Any]:
    """Validates whether LLM response conforms to JSON structure."""
    clean = response_text.strip()
    # Strip markdown ```json code blocks if present
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean)
    if match:
        clean = match.group(1).strip()

    try:
        parsed = json.loads(clean)
        return {
            "valid": True,
            "parsed": parsed,
            "error": None,
            "raw_json": clean
        }
    except json.JSONDecodeError as err:
        return {
            "valid": False,
            "parsed": None,
            "error": f"JSON syntax error at line {err.lineno}, col {err.colno}: {err.msg}",
            "raw_json": clean
        }


# ============================================================================
# 5. Dual-Model Answer Verification Critic
# ============================================================================

VERIFICATION_SYSTEM_PROMPT = """You are an impartial AI Fact-Checker, Logic Verifier, and Epistemic Critic.
Your goal is to inspect a generated answer against the user's prompt to check for:
1. Hallucinations or unsupported factual assertions.
2. Logical contradictions or faulty reasoning steps.
3. Overlooked constraints, boundary conditions, or edge cases.
4. Epistemic calibration: Does the answer claim certainty where there is ambiguity?

You must output your evaluation strictly as structured Markdown with these sections:
### 🛡️ Answer Verification & Epistemic Audit
**Confidence Score**: [0-100]% ([High / Moderate / Questionable / Critical Flaws])

- **Verified Strengths**:
  - [bullet 1]
- **Potential Flaws & Unverified Claims**:
  - [bullet 1 or 'None detected. Claims align with established principles.']
- **Overlooked Edge Cases / Nuances**:
  - [bullet 1]
- **Verdict**: [Verified Sound / Minor Nuance Missing / Requires Correction]
"""

def generate_answer_verification(
    user_prompt: str,
    assistant_reply: str,
    critic_model: Optional[str] = None,
    timeout: int = 60
) -> Dict[str, Any]:
    """Runs a secondary verification pass using a critic prompt."""
    model = critic_model or "qwen2.5:latest"
    critic_messages = [
        {"role": "system", "content": VERIFICATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"USER PROMPT:\n{user_prompt}\n\nPROPOSED ASSISTANT ANSWER:\n{assistant_reply}\n\nPlease critically audit this answer."
        }
    ]

    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": critic_messages,
                "options": {"temperature": 0.2, "top_p": 0.9, "num_predict": 2048},
                "stream": False
            },
            timeout=timeout
        )
        if r.status_code == 200:
            critique = r.json().get("message", {}).get("content", "").strip()
            # Extract score if present
            score_match = re.search(r"\*\*Confidence Score\*\*:\s*(\d+)%", critique)
            score = int(score_match.group(1)) if score_match else 90
            return {
                "ok": True,
                "score": score,
                "critique": critique,
                "critic_model": model
            }
    except Exception as e:
        return {
            "ok": False,
            "score": 0,
            "critique": f"Verification failed: {str(e)}",
            "critic_model": model
        }


# ============================================================================
# 6. Reusable Generation Presets CRUD
# ============================================================================

def list_generation_presets() -> List[Dict[str, Any]]:
    """Returns all generation presets."""
    with _get_db() as conn:
        c = conn.cursor()
        rows = c.execute("SELECT * FROM generation_presets ORDER BY is_builtin DESC, name ASC").fetchall()
        return [dict(r) for r in rows]


def get_generation_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    """Fetches a single generation preset by ID."""
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM generation_presets WHERE id = ?", (preset_id,)).fetchone()
        return dict(row) if row else None


def save_generation_preset(
    name: str,
    description: str = "",
    category: str = "general",
    temperature: float = 0.7,
    top_p: float = 0.9,
    presence_penalty: float = 0.0,
    frequency_penalty: float = 0.0,
    num_predict: int = 4096,
    thinking_level: str = "balanced",
    system_prefix: str = "",
    preset_id: Optional[str] = None
) -> Dict[str, Any]:
    """Creates or updates a generation preset."""
    pid = preset_id or f"preset_{uuid.uuid4().hex[:10]}"
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO generation_presets
            (id, name, description, category, temperature, top_p, presence_penalty,
             frequency_penalty, num_predict, thinking_level, system_prefix, is_builtin, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
        """, (
            pid, name, description, category, temperature, top_p,
            presence_penalty, frequency_penalty, num_predict, thinking_level, system_prefix
        ))
        conn.commit()
    return get_generation_preset(pid)


def delete_generation_preset(preset_id: str) -> bool:
    """Deletes a custom generation preset (built-ins cannot be deleted)."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM generation_presets WHERE id = ? AND is_builtin = 0", (preset_id,))
        conn.commit()
        return c.rowcount > 0


# ============================================================================
# 7. Request Recorder & Prompt Debugger
# ============================================================================

def record_inference_request(
    session_id: Optional[str],
    workspace_id: Optional[str],
    mode: str,
    requested_model: str,
    effective_model: str,
    task_type: str,
    routing_rationale: str,
    preset_id: Optional[str],
    messages: List[Dict[str, Any]],
    options: Dict[str, Any],
    response_content: str,
    thinking_content: str = "",
    duration_ms: int = 0,
    tokens_per_sec: float = 0.0,
    status: str = "ok",
    fallback_used: bool = False,
    structured_schema: Optional[str] = None,
    structured_valid: bool = True,
    verification_critique: Optional[str] = None
) -> str:
    """Records an inference request and prompt assembly into the debugger log."""
    req_id = f"req_{uuid.uuid4().hex[:12]}"
    system_chars = sum(len(m.get("content", "")) for m in messages if m.get("role") == "system")
    total_chars = sum(len(m.get("content", "")) for m in messages)
    token_est = int(total_chars / 3.8)

    # Count memories or knowledge snippets injected
    mem_count = 0
    fts_count = 0
    for m in messages:
        if m.get("role") == "system":
            c_text = m.get("content", "")
            if "[RELEVANT MEMORIES" in c_text:
                mem_count += c_text.count("•")
            if "[RELEVANT SOURCED KNOWLEDGE" in c_text:
                fts_count += c_text.count("Source [")

    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO request_records
            (id, session_id, workspace_id, mode, requested_model, effective_model,
             task_type, routing_rationale, preset_id, messages_json, options_json,
             system_prompt_chars, context_tokens_est, memories_injected_count,
             fts_docs_injected_count, response_preview, response_full, thinking_full,
             duration_ms, tokens_per_sec, status, fallback_used, structured_schema,
             structured_valid, verification_critique)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            req_id, session_id, workspace_id, mode, requested_model, effective_model,
            task_type, routing_rationale, preset_id, json.dumps(messages), json.dumps(options),
            system_chars, token_est, mem_count, fts_count,
            response_content[:300], response_content, thinking_content,
            duration_ms, tokens_per_sec, status, 1 if fallback_used else 0,
            structured_schema, 1 if structured_valid else 0, verification_critique
        ))
        conn.commit()
    return req_id


def list_request_records(limit: int = 50, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns recent request records for the Prompt Debugger."""
    with _get_db() as conn:
        c = conn.cursor()
        if session_id:
            rows = c.execute("""
                SELECT id, session_id, workspace_id, mode, requested_model, effective_model,
                       task_type, routing_rationale, preset_id, system_prompt_chars,
                       context_tokens_est, memories_injected_count, fts_docs_injected_count,
                       response_preview, duration_ms, tokens_per_sec, status, fallback_used,
                       structured_valid, (verification_critique IS NOT NULL) AS has_critique,
                       created_at
                FROM request_records
                WHERE session_id = ?
                ORDER BY created_at DESC LIMIT ?
            """, (session_id, limit)).fetchall()
        else:
            rows = c.execute("""
                SELECT id, session_id, workspace_id, mode, requested_model, effective_model,
                       task_type, routing_rationale, preset_id, system_prompt_chars,
                       context_tokens_est, memories_injected_count, fts_docs_injected_count,
                       response_preview, duration_ms, tokens_per_sec, status, fallback_used,
                       structured_valid, (verification_critique IS NOT NULL) AS has_critique,
                       created_at
                FROM request_records
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_request_record(req_id: str) -> Optional[Dict[str, Any]]:
    """Fetches full payload of a request record."""
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM request_records WHERE id = ?", (req_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["messages"] = json.loads(d["messages_json"]) if d.get("messages_json") else []
        d["options"] = json.loads(d["options_json"]) if d.get("options_json") else {}
        return d


def diff_request_records(req_id_a: str, req_id_b: str) -> Dict[str, Any]:
    """Compares two request records side-by-side for prompt debugging."""
    a = get_request_record(req_id_a)
    b = get_request_record(req_id_b)
    if not a or not b:
        return {"error": "One or both request records not found"}

    return {
        "record_a": {
            "id": a["id"],
            "model": a["effective_model"],
            "task_type": a["task_type"],
            "preset_id": a["preset_id"],
            "duration_ms": a["duration_ms"],
            "tokens_per_sec": a["tokens_per_sec"],
            "context_tokens_est": a["context_tokens_est"],
            "system_prompt_chars": a["system_prompt_chars"],
            "options": a["options"],
            "response_preview": a["response_preview"],
            "created_at": a["created_at"]
        },
        "record_b": {
            "id": b["id"],
            "model": b["effective_model"],
            "task_type": b["task_type"],
            "preset_id": b["preset_id"],
            "duration_ms": b["duration_ms"],
            "tokens_per_sec": b["tokens_per_sec"],
            "context_tokens_est": b["context_tokens_est"],
            "system_prompt_chars": b["system_prompt_chars"],
            "options": b["options"],
            "response_preview": b["response_preview"],
            "created_at": b["created_at"]
        },
        "options_diff": {
            k: {"a": a["options"].get(k), "b": b["options"].get(k)}
            for k in set(a["options"].keys()).union(set(b["options"].keys()))
            if a["options"].get(k) != b["options"].get(k)
        }
    }
