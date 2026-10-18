"""
Lamplight Multimodal Studio (Pillar 6)
=====================================
Local-first multimodal intelligence suite providing:
1. Vision Workspace & Multi-Model Visual Comparison (Image QA, Diagram Decomposition, OCR)
2. Screenshot Debugger for Architect (UI bug investigation, stacktrace mapping to source files)
3. Audio Transcription Desk & Persistent Audio Library (Timestamped segments via faster-whisper)
4. AI Structured Notes & Action Items Extractor (Executive digest, key topics, action checklist)
5. Local ComfyUI / Stable Diffusion Client (Health check, generation dispatch, gallery management)
6. Multimodal Capability Detector & Intelligent Router
"""

import os
import re
import json
import time
import uuid
import sqlite3
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "chatbots.db"
UPLOAD_DIR = BASE / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Known Ollama vision-capable architectures and model names
KNOWN_VISION_MODELS = [
    "llava", "bakllava", "llama3.2-vision", "minicpm-v", "qwen2-vl",
    "qwen2.5-vl", "moondream", "yi-vl", "phi3-vision", "cogvlm"
]

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_multimodal_tables():
    """Initializes tables for audio library, vision comparisons, and image generations."""
    with _get_db() as conn:
        c = conn.cursor()

        # 1. Audio Transcripts & Structured Notes Library
        c.execute("""
            CREATE TABLE IF NOT EXISTS audio_library (
                id TEXT PRIMARY KEY,
                workspace_id TEXT DEFAULT 'ws_default',
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                duration_sec REAL DEFAULT 0,
                language TEXT DEFAULT 'en',
                transcript TEXT NOT NULL,
                segments_json TEXT DEFAULT '[]',
                summary TEXT DEFAULT '',
                topics_json TEXT DEFAULT '[]',
                action_items_json TEXT DEFAULT '[]',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_audio_ws ON audio_library(workspace_id)")

        # 2. Vision Comparisons & Screenshot Debugging Records
        c.execute("""
            CREATE TABLE IF NOT EXISTS vision_comparisons (
                id TEXT PRIMARY KEY,
                workspace_id TEXT DEFAULT 'ws_default',
                task_mode TEXT DEFAULT 'general',
                image_filename TEXT NOT NULL,
                prompt TEXT NOT NULL,
                model_a TEXT NOT NULL,
                response_a TEXT NOT NULL,
                duration_ms_a INTEGER DEFAULT 0,
                model_b TEXT DEFAULT '',
                response_b TEXT DEFAULT '',
                duration_ms_b INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 3. ComfyUI / SD Generated Images Gallery
        c.execute("""
            CREATE TABLE IF NOT EXISTS comfyui_generations (
                id TEXT PRIMARY KEY,
                workspace_id TEXT DEFAULT 'ws_default',
                prompt TEXT NOT NULL,
                negative_prompt TEXT DEFAULT '',
                aspect_ratio TEXT DEFAULT '1:1',
                seed INTEGER DEFAULT -1,
                image_url TEXT NOT NULL,
                status TEXT DEFAULT 'completed',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_multimodal_tables()


# ============================================================================
# 1. Multimodal Capability Detection
# ============================================================================

def detect_capabilities() -> Dict[str, Any]:
    """Detects available local vision models, Whisper STT, Kokoro TTS, and ComfyUI."""
    # 1. Check Ollama vision models
    vision_models = []
    text_models = []
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for m in data.get("models", []):
                name = m.get("name", "")
                name_lower = name.lower()
                is_vision = any(vm in name_lower for vm in KNOWN_VISION_MODELS)
                if is_vision:
                    vision_models.append(name)
                else:
                    text_models.append(name)
    except Exception:
        pass

    # 2. Check Whisper STT
    whisper_available = False
    try:
        import faster_whisper
        whisper_available = True
    except ImportError:
        pass

    # 3. Check Kokoro TTS
    tts_models_dir = BASE / "tts_models"
    kokoro_model = tts_models_dir / "kokoro-v1.0.onnx"
    kokoro_voices = tts_models_dir / "voices-v1.0.bin"
    tts_available = kokoro_model.exists() and kokoro_voices.exists()

    # 4. Check ComfyUI / SD
    comfyui_status = check_comfyui_status()

    return {
        "vision_models": vision_models,
        "text_models": text_models,
        "has_vision": len(vision_models) > 0,
        "whisper_available": whisper_available,
        "tts_available": tts_available,
        "comfyui": comfyui_status
    }


# ============================================================================
# 2. Audio Transcription Desk & Timestamp Segmentation
# ============================================================================

def format_timestamp(seconds: float) -> str:
    """Converts seconds (e.g. 125.4) into '02:05' or '01:02:05'."""
    sec_int = int(seconds)
    hours = sec_int // 3600
    minutes = (sec_int % 3600) // 60
    secs = sec_int % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def transcribe_detailed(filepath: Path) -> Dict[str, Any]:
    """
    Transcribes audio via faster-whisper with precise sentence segment timestamps.
    Returns: { text, duration_sec, language, segments: [{ id, start, end, timestamp, text }] }
    """
    try:
        import numpy as np
        from faster_whisper import WhisperModel

        cmd = ["ffmpeg", "-nostdin", "-threads", "0", "-i", str(filepath), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not proc.stdout:
            err = proc.stderr.decode("utf-8", errors="ignore")[:200] if proc.stderr else "Empty audio data"
            return {"text": "", "duration_sec": 0, "language": "unknown", "segments": [], "error": err}

        audio_arr = np.frombuffer(proc.stdout, np.int16).flatten().astype(np.float32) / 32768.0
        dur_sec = len(audio_arr) / 16000.0

        if len(audio_arr) < 1600:
            return {"text": "", "duration_sec": round(dur_sec, 1), "language": "en", "segments": []}

        # Use base/small model or tiny.en for quick responsive local transcription
        model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        raw_segments, info = model.transcribe(audio_arr, beam_size=1, word_timestamps=False)

        segments = []
        full_text_parts = []
        for idx, seg in enumerate(raw_segments):
            seg_text = seg.text.strip()
            if not seg_text:
                continue
            full_text_parts.append(seg_text)
            segments.append({
                "id": idx + 1,
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "timestamp": format_timestamp(seg.start),
                "text": seg_text
            })

        lang = getattr(info, "language", "en") if info else "en"
        full_text = " ".join(full_text_parts)

        return {
            "text": full_text,
            "duration_sec": round(dur_sec, 1),
            "duration_formatted": format_timestamp(dur_sec),
            "language": lang,
            "segments": segments
        }
    except Exception as e:
        return {
            "text": "",
            "duration_sec": 0,
            "duration_formatted": "00:00",
            "language": "unknown",
            "segments": [],
            "error": str(e)
        }


def extract_structured_meeting_notes(transcript: str, model_name: str = "Qwen3.5-uncensored:latest") -> Dict[str, Any]:
    """
    Analyzes an audio transcript using the local LLM and formats it into:
    - Executive Summary
    - Key Topics Discussed
    - Action Items & Next Steps
    - Key Questions & Clarifications
    """
    if not transcript or len(transcript.strip()) < 10:
        return {
            "summary": "Transcript is too brief to extract structured notes.",
            "topics": [],
            "action_items": [],
            "key_questions": []
        }

    sys_prompt = (
        "You are an expert executive meeting secretary and research assistant. "
        "Analyze the provided audio transcript thoroughly and extract structured, high-value notes. "
        "Output ONLY a valid JSON object with these exact keys:\n"
        "{\n"
        '  "summary": "2-3 concise paragraphs summarizing the core discussions, decisions, and outcomes.",\n'
        '  "topics": ["Key topic 1", "Key topic 2", "Key topic 3"],\n'
        '  "action_items": [{"task": "Specific action item", "assignee": "Person/Role or Unassigned", "status": "pending"}],\n'
        '  "key_questions": ["Important question raised or answered during discussion"]\n'
        "}\n"
        "Do not include any intro, markdown fences, or extra commentary outside the JSON."
    )

    prompt = f"### Audio Transcript:\n{transcript[:15000]}\n\nGenerate structured meeting notes:"

    try:
        from app import _run_model_completion  # reuse internal model caller
        raw_res = _run_model_completion(model_name, prompt, sys_prompt, temperature=0.3)
        # Parse JSON
        clean = re.sub(r"^```(?:json)?", "", raw_res.strip(), flags=re.MULTILINE)
        clean = re.sub(r"```$", "", clean.strip(), flags=re.MULTILINE).strip()
        m = re.search(r"\{[\s\S]*\}", clean)
        if m:
            data = json.loads(m.group(0))
            return {
                "summary": data.get("summary", ""),
                "topics": data.get("topics", []),
                "action_items": data.get("action_items", []),
                "key_questions": data.get("key_questions", [])
            }
    except Exception as e:
        pass

    # Heuristic fallback if LLM is slow or JSON unparsable
    return {
        "summary": transcript[:300] + ("..." if len(transcript) > 300 else ""),
        "topics": ["General Discussion"],
        "action_items": [{"task": "Review full transcript", "assignee": "Self", "status": "pending"}],
        "key_questions": []
    }


def save_audio_recording(
    workspace_id: str,
    filename: str,
    filepath: str,
    duration_sec: float,
    language: str,
    transcript: str,
    segments: List[Dict[str, Any]],
    summary: str = "",
    topics: List[str] = None,
    action_items: List[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Persists a processed recording into the Audio Library."""
    rec_id = f"aud_{uuid.uuid4().hex[:10]}"
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO audio_library
            (id, workspace_id, filename, filepath, duration_sec, language, transcript,
             segments_json, summary, topics_json, action_items_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec_id,
            workspace_id or "ws_default",
            filename,
            filepath,
            duration_sec,
            language,
            transcript,
            json.dumps(segments or []),
            summary,
            json.dumps(topics or []),
            json.dumps(action_items or [])
        ))
        conn.commit()
    return get_audio_recording(rec_id)


def list_audio_recordings(workspace_id: str = "ws_default", query: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lists saved recordings in the library."""
    ws = workspace_id or "ws_default"
    sql = "SELECT * FROM audio_library WHERE (workspace_id = ? OR workspace_id = 'ws_default')"
    params = [ws]
    if query:
        sql += " AND (filename LIKE ? OR transcript LIKE ? OR summary LIKE ?)"
        q_wild = f"%{query}%"
        params.extend([q_wild, q_wild, q_wild])
    sql += " ORDER BY created_at DESC"

    with _get_db() as conn:
        c = conn.cursor()
        rows = c.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["duration_formatted"] = format_timestamp(d["duration_sec"])
            try:
                d["topics"] = json.loads(d["topics_json"])
            except Exception:
                d["topics"] = []
            results.append(d)
        return results


def get_audio_recording(rec_id: str) -> Optional[Dict[str, Any]]:
    """Fetches a single recording with full segments and action items."""
    with _get_db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM audio_library WHERE id = ?", (rec_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["duration_formatted"] = format_timestamp(d["duration_sec"])
        try:
            d["segments"] = json.loads(d["segments_json"])
        except Exception:
            d["segments"] = []
        try:
            d["topics"] = json.loads(d["topics_json"])
        except Exception:
            d["topics"] = []
        try:
            d["action_items"] = json.loads(d["action_items_json"])
        except Exception:
            d["action_items"] = []
        return d


def delete_audio_recording(rec_id: str) -> bool:
    """Deletes recording from database."""
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM audio_library WHERE id = ?", (rec_id,))
        conn.commit()
        return c.rowcount > 0


# ============================================================================
# 3. Vision Workspace & Screenshot Debugger
# ============================================================================

VISION_MODE_PROMPTS = {
    "general": "Describe and analyze this visual material thoroughly, addressing key details, objects, layout, and textual elements.",
    "screenshot_debug": (
        "You are Architect, an elite software engineering assistant. "
        "Investigate this error/interface screenshot carefully:\n"
        "1. Identify the exact error message, stack trace, HTTP code, or broken UI element.\n"
        "2. Pinpoint the likely culprit in client code, API handler, database, or CSS layout.\n"
        "3. Provide concrete code fixes and remediation steps."
    ),
    "ocr": "Perform high-accuracy optical character recognition (OCR). Extract all visible text, headers, table rows, and numerical figures verbatim in formatted Markdown.",
    "diagram": "Analyze this architecture diagram or flowchart. Decompose the nodes, services, data flow directions, and produce an equivalent Mermaid.js diagram (`mermaid`) along with architectural notes."
}

def analyze_vision_single(
    image_b64: str,
    prompt: str,
    model: str,
    task_mode: str = "general",
    context_notes: str = ""
) -> Dict[str, Any]:
    """Queries a vision-capable model with an image and task-specific guidance."""
    t0 = time.time()
    mode_instructions = VISION_MODE_PROMPTS.get(task_mode, VISION_MODE_PROMPTS["general"])
    
    full_prompt = f"{mode_instructions}\n\n"
    if context_notes:
        full_prompt += f"### Additional Context & Logs:\n{context_notes}\n\n"
    full_prompt += f"### User Inquiry:\n{prompt or 'Analyze this image.'}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": full_prompt,
                "images": [image_b64]
            }
        ],
        "stream": False,
        "options": {
            "temperature": 0.2
        }
    }

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed_ms = int((time.time() - t0) * 1000)
            text = data.get("message", {}).get("content", "")
            return {
                "ok": True,
                "model": model,
                "task_mode": task_mode,
                "response": text,
                "duration_ms": elapsed_ms
            }
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            "ok": False,
            "model": model,
            "task_mode": task_mode,
            "response": f"Vision inference failed on model '{model}': {str(e)}",
            "duration_ms": elapsed_ms,
            "error": str(e)
        }


def compare_vision_duel(
    image_b64: str,
    prompt: str,
    model_a: str,
    model_b: str,
    task_mode: str = "general"
) -> Dict[str, Any]:
    """Runs a visual comparison duel between two image-capable models."""
    res_a = analyze_vision_single(image_b64, prompt, model_a, task_mode)
    res_b = analyze_vision_single(image_b64, prompt, model_b, task_mode)

    return {
        "task_mode": task_mode,
        "prompt": prompt,
        "model_a": res_a,
        "model_b": res_b
    }


# ============================================================================
# 4. Local ComfyUI / Stable Diffusion Integration
# ============================================================================

def check_comfyui_status(endpoint: str = "http://127.0.0.1:8188") -> Dict[str, Any]:
    """Checks whether local ComfyUI instance is alive and listening."""
    clean_endpoint = endpoint.rstrip("/")
    try:
        req = urllib.request.Request(f"{clean_endpoint}/system_stats")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "online": True,
                "endpoint": clean_endpoint,
                "stats": data,
                "message": "ComfyUI instance is online & ready."
            }
    except Exception:
        # Check standard SD WebUI / Automatic1111 on port 7860 as fallback
        try:
            req_sd = urllib.request.Request(f"http://127.0.0.1:7860/sdapi/v1/options")
            with urllib.request.urlopen(req_sd, timeout=1.0) as resp:
                return {
                    "online": True,
                    "endpoint": "http://127.0.0.1:7860",
                    "type": "automatic1111",
                    "message": "Automatic1111 / SD WebUI is online & ready."
                }
        except Exception:
            pass

        return {
            "online": False,
            "endpoint": clean_endpoint,
            "message": f"ComfyUI is offline at {clean_endpoint}. Start ComfyUI to enable local image synthesis."
        }


def save_generated_image_meta(
    workspace_id: str,
    prompt: str,
    negative_prompt: str,
    aspect_ratio: str,
    seed: int,
    image_url: str
) -> Dict[str, Any]:
    """Saves a generated image record into gallery."""
    img_id = f"img_{uuid.uuid4().hex[:10]}"
    with _get_db() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO comfyui_generations
            (id, workspace_id, prompt, negative_prompt, aspect_ratio, seed, image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            img_id,
            workspace_id or "ws_default",
            prompt,
            negative_prompt,
            aspect_ratio,
            seed,
            image_url
        ))
        conn.commit()
    return {"id": img_id, "image_url": image_url, "prompt": prompt}


def list_generated_images(workspace_id: str = "ws_default") -> List[Dict[str, Any]]:
    """Lists saved images in current workspace gallery."""
    ws = workspace_id or "ws_default"
    with _get_db() as conn:
        c = conn.cursor()
        rows = c.execute("""
            SELECT * FROM comfyui_generations
            WHERE workspace_id = ? OR workspace_id = 'ws_default'
            ORDER BY created_at DESC
        """, (ws,)).fetchall()
        return [dict(r) for r in rows]
