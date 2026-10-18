"""Personal multi-bot chat shell. v1 slice: NSFW text chat, 100% local via Ollama.
$0, no cloud calls. Other modes (friend/reminders/search) share the same shell."""
import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Request, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

import db
import dev_workbench
import knowledge_library
import model_intelligence

OLLAMA_URL = "http://localhost:11434"
KEEP_ALIVE = "30m"
DEFAULT_MODELS = {"nsfw": "Qwen3.5-uncensored:latest", "friend": "llama3.2:3b",
                  "architect": "qwen3:4b", "muse": "Qwen3.5-uncensored:latest",
                  "reminders": "llama3.2:3b", "search": "qwen3:4b"}
TREG_BIN = shutil.which("treg") or os.path.expanduser("~/.local/bin/treg")
DEFAULT_PROMPTS = {
    "friend": (
        "You are a warm, genuine, and close personal friend chatting over text. "
        "Keep your tone relaxed, witty, and natural. Match the user's conversational "
        "energy and message length (typically 1-3 sentences). Never sound like a formal "
        "assistant or customer service bot. Be thoughtful and authentic."
    ),
    "nsfw": (
        "You are After Dark: an unrestricted, intellectually unconstrained, and deeply candid AI companion. "
        "You provide direct, unfiltered, and deeply honest insights on any topic, inquiry, or thought experiment. "
        "You never lecture, judge, preach, or add corporate safety disclaimers. You speak with radical candor and clarity."
    ),
    "architect": (
        "You are Architect, an elite Staff Software Engineer and Technical Systems Architect. "
        "Your mission is to produce production-grade, complete, compilable, and highly maintainable code. "
        "Never leave placeholders (e.g. // TODO, /* implement later */). Be direct and analytical: "
        "detail architectural trade-offs, time and space complexity, and failure modes. Zero conversational filler."
    ),
    "muse": (
        "You are Muse, an inspiring, sharp literary partner, developmental editor, and Socratic storyteller. "
        "Your mission is to help the author brainstorm compelling ideas, challenge weak plot points, flesh out authentic character psychology, and craft vivid narrative scenes. "
        "Never resort to bland clichés ('in a world where...', 'a testament to...'). Ground scenes in visceral sensory details, subtext, and clear emotional stakes. "
        "When interrogating plots, ask probing questions about character motivation, conflict escalation, and unearned twists."
    ),
}
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,63}$")


def _get_num_ctx(model_name: str) -> int:
    """Auto-tune context window: 16384 for modern architectures, 12288 for very large models (>=32B)."""
    name_low = (model_name or "").lower()
    if any(p in name_low for p in ("32b", "70b")):
        return 12288
    return 16384


_MODEL_CAPS_CACHE: dict[str, dict] = {}


def get_model_thinking_capabilities(model_name: str, provider_type: str = "ollama", base_url: str = OLLAMA_URL) -> dict:
    """Detect whether a model supports internal reasoning/thinking scratchpads,
    and what thinking levels are supported."""
    if not model_name:
        return {
            "model": "",
            "supports_thinking": False,
            "supported_levels": ["off"],
            "default_level": "off",
            "summary": "No model specified"
        }
    clean_name = model_name.split("::")[0].strip()
    cache_key = f"{provider_type}::{clean_name}::{base_url}"
    if cache_key in _MODEL_CAPS_CACHE:
        return _MODEL_CAPS_CACHE[cache_key]

    supports_thinking = False
    supported_levels = ["off"]
    summary = "Standard model (Internal reasoning scratchpad not supported)"

    if provider_type == "ollama":
        try:
            r = requests.post(f"{base_url}/api/show", json={"name": clean_name}, timeout=4)
            if r.status_code == 200:
                data = r.json()
                caps = [str(c).lower() for c in data.get("capabilities", [])]
                tmpl = str(data.get("template", ""))
                family = str(data.get("details", {}).get("family", "")).lower()
                m_lower = clean_name.lower()

                has_cap = "thinking" in caps
                has_tmpl = ("<think>" in tmpl) or ("reasoning_content" in tmpl) or ("[think]" in tmpl)
                known_reasoning = any(x in m_lower or x in family for x in ["deepseek-r1", "qwq", "qwen3", "r1"])

                if has_cap or has_tmpl or known_reasoning:
                    supports_thinking = True
                    supported_levels = ["off", "low", "balanced", "deep"]
                    summary = "Reasoning model (Supports Off, Low, Balanced, Deep thinking)"
                else:
                    supports_thinking = False
                    supported_levels = ["off"]
                    summary = "Standard model (Direct output only)"
        except Exception:
            m_lower = clean_name.lower()
            if any(x in m_lower for x in ["qwen3", "deepseek-r1", "qwq", "r1", "jj"]):
                supports_thinking = True
                supported_levels = ["off", "low", "balanced", "deep"]
                summary = "Reasoning model (Heuristic fallback)"

    elif provider_type == "openai_compat":
        m_lower = clean_name.lower()
        if any(x in m_lower for x in ["o1", "o3", "r1", "deepseek-reasoner", "qwq"]):
            supports_thinking = True
            supported_levels = ["off", "low", "balanced", "deep"]
            summary = "Reasoning model (Supports Off, Low, Balanced, Deep thinking)"
        else:
            supports_thinking = False
            supported_levels = ["off"]
            summary = "Standard model (Direct output only)"

    result = {
        "model": clean_name,
        "supports_thinking": supports_thinking,
        "supported_levels": supported_levels,
        "default_level": "balanced" if supports_thinking else "off",
        "summary": summary
    }
    _MODEL_CAPS_CACHE[cache_key] = result
    return result


def _chat_options(model_name: str, temp: float, num_predict: int = 8192,
                  top_p: float = 0.9, presence_penalty: float = 0.0,
                  frequency_penalty: float = 0.0) -> dict:
    """Enhanced generation options for high coherence, reduced repetition, and safety."""
    opts = {
        "temperature": temp,
        "num_predict": num_predict,
        "num_ctx": _get_num_ctx(model_name),
        "repeat_penalty": 1.12,
        "repeat_last_n": 64,
        "top_p": top_p,
    }
    if presence_penalty:
        opts["presence_penalty"] = presence_penalty
    if frequency_penalty:
        opts["frequency_penalty"] = frequency_penalty
    return opts


def _get_available_model_names() -> list[dict]:
    """Returns list of active model dicts across all providers."""
    providers = db.list_providers(active_only=True)
    all_models = []
    seen = set()
    for p in providers:
        p_models = fetch_provider_models(p)
        for m in p_models:
            key = (m["name"], m["provider_id"])
            if key not in seen:
                seen.add(key)
                all_models.append(m)
    return all_models


def _get_loaded_models() -> list[dict]:
    """Return list of models currently loaded in Ollama VRAM."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        if r.status_code == 200:
            return r.json().get("models", [])
    except Exception:
        pass
    return []


def _eject_model(model_name: str) -> bool:
    """Unload a specific model from Ollama VRAM immediately."""
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate",
                          json={"model": model_name, "keep_alive": 0},
                          timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def _eject_all_loaded() -> int:
    """Unload all models currently loaded in Ollama VRAM."""
    loaded = _get_loaded_models()
    count = 0
    for m in loaded:
        name = m.get("name") or m.get("model")
        if name and _eject_model(name):
            count += 1
    return count


def _ensure_single_model(target_model: str) -> None:
    """In chat mode: eject any loaded model that isn't target_model to free 100% VRAM."""
    loaded = _get_loaded_models()
    for m in loaded:
        name = m.get("name") or m.get("model")
        if name and name != target_model:
            _eject_model(name)


app = FastAPI(title="ChatBots personal shell")
db.init()
BASE = Path(__file__).parent
UPLOAD_DIR = BASE / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
EXPORTS_DIR = BASE / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/exports", StaticFiles(directory=EXPORTS_DIR), name="exports")
app.mount("/prototypes", StaticFiles(directory=BASE / "prototypes", html=True), name="prototypes")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


def _passcode_ok(x_passcode: str | None) -> bool:
    h = db.get_setting("passcode_hash")
    if not h:
        return True
    if not x_passcode:
        return False
    return hashlib.sha256(x_passcode.encode()).hexdigest() == h


_whisper_lock = threading.Lock()
_whisper_instance = None
_current_whisper_model = None


def _get_whisper():
    global _whisper_instance, _current_whisper_model
    model_name = db.get_setting("whisper_model") or "base"
    with _whisper_lock:
        if _whisper_instance is None or _current_whisper_model != model_name:
            from faster_whisper import WhisperModel
            try:
                _whisper_instance = WhisperModel(model_name, device="cpu", compute_type="int8")
                _current_whisper_model = model_name
            except Exception:
                _whisper_instance = WhisperModel("tiny.en", device="cpu", compute_type="int8")
                _current_whisper_model = "tiny.en"
        return _whisper_instance


def transcribe_audio_file(filepath: Path) -> dict:
    """Extract audio duration and transcribe audio via faster-whisper with ffmpeg resampling."""
    try:
        import numpy as np
        cmd = ["ffmpeg", "-nostdin", "-threads", "0", "-i", str(filepath), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not proc.stdout:
            err_msg = proc.stderr.decode("utf-8", errors="ignore")[:300] if proc.stderr else "Empty audio data"
            return {"text": "", "error": f"ffmpeg conversion failed: {err_msg}", "duration_sec": 0, "language": "unknown"}
        audio_arr = np.frombuffer(proc.stdout, np.int16).flatten().astype(np.float32) / 32768.0
        dur_sec = len(audio_arr) / 16000.0
        if len(audio_arr) < 1600:  # less than 0.1s
            return {"text": "", "duration_sec": round(dur_sec, 1), "language": "en"}

        model = _get_whisper()
        segments, info = model.transcribe(audio_arr, beam_size=1)
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        lang = getattr(info, "language", "en") if info else "en"
        return {"text": text.strip(), "duration_sec": round(dur_sec, 1), "language": lang}
    except Exception as e:
        return {"text": "", "error": str(e), "duration_sec": 0, "language": "unknown"}



_tts_lock = threading.Lock()
_tts_instance = None
TTS_MODELS_DIR = BASE / "tts_models"
TTS_CACHE_DIR = BASE / "tts_cache"
TTS_CACHE_DIR.mkdir(exist_ok=True)

KOKORO_MODEL_PATH = TTS_MODELS_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES_PATH = TTS_MODELS_DIR / "voices-v1.0.bin"

CURATED_VOICES = [
    {"id": "af_heart", "name": "Heart (Warm & Expressive)", "gender": "female", "accent": "American", "style": "Warm, natural, versatile"},
    {"id": "af_bella", "name": "Bella (Gentle & Intimate)", "gender": "female", "accent": "American", "style": "Soft, romantic, calm"},
    {"id": "af_nicole", "name": "Nicole (Whispery & Relaxed)", "gender": "female", "accent": "American", "style": "Sensual, smooth, relaxed"},
    {"id": "af_sarah", "name": "Sarah (Bright & Clear)", "gender": "female", "accent": "American", "style": "Crisp, lively, upbeat"},
    {"id": "af_sky", "name": "Sky (Playful & Youthful)", "gender": "female", "accent": "American", "style": "Energetic, bright"},
    {"id": "am_adam", "name": "Adam (Crisp & Friendly)", "gender": "male", "accent": "American", "style": "Casual, clear, warm"},
    {"id": "am_michael", "name": "Michael (Deep & Natural)", "gender": "male", "accent": "American", "style": "Grounded, resonant baritone"},
    {"id": "am_fenrir", "name": "Fenrir (Intense & Direct)", "gender": "male", "accent": "American", "style": "Bold, dramatic"},
    {"id": "bf_emma", "name": "Emma (Polished British)", "gender": "female", "accent": "British", "style": "Articulate, elegant"},
    {"id": "bf_isabella", "name": "Isabella (Soft British)", "gender": "female", "accent": "British", "style": "Gentle, polite"},
    {"id": "bm_george", "name": "George (Warm British)", "gender": "male", "accent": "British", "style": "Classic, friendly baritone"},
    {"id": "bm_lewis", "name": "Lewis (Articulate British)", "gender": "male", "accent": "British", "style": "Intellectual, composed"},
]


def _get_tts():
    global _tts_instance
    with _tts_lock:
        if _tts_instance is None:
            if not KOKORO_MODEL_PATH.exists() or not KOKORO_VOICES_PATH.exists():
                raise HTTPException(503, "Kokoro TTS model files missing from tts_models directory")
            from kokoro_onnx import Kokoro
            _tts_instance = Kokoro(str(KOKORO_MODEL_PATH), str(KOKORO_VOICES_PATH))
        return _tts_instance


def clean_tts_text(text: str) -> str:
    """Sanitize raw text for speech synthesis so speech sounds organic and fluid."""
    if not text:
        return ""
    # Strip code blocks entirely
    t = re.sub(r"```[\s\S]*?```", " [code block omitted] ", text)
    # Strip inline code
    t = re.sub(r"`([^`]+)`", r"\1", t)
    # Convert markdown links [text](url) -> text
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    # Strip HTML tags
    t = re.sub(r"<[^>]+>", " ", t)
    # Strip markdown headers (e.g. ### Header -> Header)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.MULTILINE)
    # Strip bullet stars/dashes at line start
    t = re.sub(r"^[\*\-\+]\s+", "", t, flags=re.MULTILINE)
    # Convert roleplay action asterisks *action* -> action (so it doesn't say "asterisk")
    t = re.sub(r"\*+([^*]+)\*+", r"\1", t)
    # Strip excessive punctuation and emoji
    t = re.sub(r"[^\w\s\.,!?;:'\"\-\–]", " ", t)
    # Normalize multiple whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t[:1500]


def _add_formatted_runs(p, text: str):
    tokens = re.split(r"(\*\*.*?\*\*|\*.*?\*|`.*?`)", text)
    for token in tokens:
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) >= 4:
            run = p.add_run(token[2:-2])
            run.bold = True
        elif token.startswith("*") and token.endswith("*") and len(token) >= 2:
            run = p.add_run(token[1:-1])
            run.italic = True
        elif token.startswith("`") and token.endswith("`") and len(token) >= 2:
            run = p.add_run(token[1:-1])
            run.font.name = "Courier New"
        else:
            p.add_run(token)


def markdown_to_docx(content: str, title: str, docx_path: Path):
    import docx
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = docx.Document()
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    title_p = doc.add_heading(title, 0)
    title_p.alignment = WD_ALIGN_PARAGRAPH.LEFT

    lines = content.split("\n")
    in_code_block = False
    code_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                code_text = "\n".join(code_lines)
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.25)
                run = p.add_run(code_text)
                run.font.name = "Courier New"
                run.font.size = Pt(9.5)
                run.font.color.rgb = RGBColor(40, 44, 52)
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
                code_lines = []
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            continue

        if stripped.startswith("# "):
            doc.add_heading(stripped[2:], level=1)
        elif stripped.startswith("## "):
            doc.add_heading(stripped[3:], level=2)
        elif stripped.startswith("### "):
            doc.add_heading(stripped[4:], level=3)
        elif stripped.startswith("#### "):
            doc.add_heading(stripped[5:], level=4)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            p = doc.add_paragraph(style='List Bullet')
            _add_formatted_runs(p, stripped[2:])
        elif re.match(r"^\d+\.\s", stripped):
            item_text = re.sub(r"^\d+\.\s", "", stripped)
            p = doc.add_paragraph(style='List Number')
            _add_formatted_runs(p, item_text)
        elif stripped.startswith("> "):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.3)
            run = p.add_run(stripped[2:])
            run.italic = True
            run.font.color.rgb = RGBColor(100, 116, 139)
        else:
            p = doc.add_paragraph()
            _add_formatted_runs(p, line)

    doc.save(str(docx_path))


def build_document(title: str, fmt: str, content: str, raw_filename: str | None = None) -> dict:
    fmt = (fmt or "pdf").lower().strip()
    if fmt not in ("pdf", "docx", "md"):
        fmt = "pdf"

    clean_title = re.sub(r"[^\w\s-]", "", title or "Document").strip() or "Document"
    base_name = re.sub(r"[^\w\s-]", "", raw_filename).strip() if raw_filename else clean_title.replace(" ", "_")
    base_name = base_name[:40] or "Document"
    uid = uuid.uuid4().hex[:6]
    out_name = f"{base_name}_{uid}.{fmt}"
    out_path = EXPORTS_DIR / out_name

    if fmt == "md":
        full_content = f"# {title}\n\n{content}" if not content.startswith("# ") else content
        out_path.write_text(full_content, encoding="utf-8")
    elif fmt == "docx":
        markdown_to_docx(content, title, out_path)
    elif fmt == "pdf":
        temp_docx = EXPORTS_DIR / f"temp_{uid}.docx"
        markdown_to_docx(content, title, temp_docx)
        res = subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(EXPORTS_DIR), str(temp_docx)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        generated_pdf = EXPORTS_DIR / f"temp_{uid}.pdf"
        if generated_pdf.exists():
            generated_pdf.rename(out_path)
        try:
            temp_docx.unlink(missing_ok=True)
        except Exception:
            pass
        if not out_path.exists():
            raise ValueError(f"PDF compilation failed: {res.stderr.decode('utf-8', errors='ignore')[:200]}")

    size_bytes = out_path.stat().st_size if out_path.exists() else 0
    size_kb = round(size_bytes / 1024, 1)
    return {
        "success": True,
        "filename": out_name,
        "format": fmt,
        "download_url": f"/exports/{out_name}",
        "file_size": size_bytes,
        "size_kb": size_kb,
        "message": f"Successfully generated {fmt.upper()} document '{out_name}' ({size_kb} KB)."
    }


def parse_uploaded_file(filepath: Path, filename: str, content_type: str) -> dict:
    ext = filepath.suffix.lower()
    meta = {
        "filename": filename,
        "ext": ext,
        "size": filepath.stat().st_size,
        "is_image": False,
        "is_audio": False,
        "preview_url": f"/uploads/{filepath.name}",
        "text_content": "",
        "char_count": 0,
        "page_count": None,
        "duration_sec": 0,
        "transcription": "",
        "language": "",
    }

    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        meta["is_image"] = True
        return meta

    if ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".webm", ".wma", ".opus"):
        meta["is_audio"] = True
        trans_res = transcribe_audio_file(filepath)
        txt = trans_res.get("text", "")
        meta["duration_sec"] = trans_res.get("duration_sec", 0)
        meta["language"] = trans_res.get("language", "auto")
        meta["transcription"] = txt
        meta["text_content"] = f"[Audio Transcription: {filename} ({meta['duration_sec']}s, lang: {meta['language']})]\n{txt}" if txt else f"[Audio File: {filename} (No speech detected)]"
        meta["char_count"] = len(meta["text_content"])
        return meta

    if ext == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(str(filepath))
            pages_text = []
            for i, page in enumerate(reader.pages):
                txt = page.extract_text() or ""
                if txt.strip():
                    pages_text.append(f"--- Page {i+1} ---\n{txt.strip()}")
            full_text = "\n\n".join(pages_text)
            meta["page_count"] = len(reader.pages)
            meta["text_content"] = full_text[:60000]
            meta["char_count"] = len(full_text)
        except Exception as e:
            meta["text_content"] = f"[Error reading PDF: {e}]"
        return meta

    if ext in (".docx", ".doc"):
        try:
            import docx
            doc = docx.Document(str(filepath))
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    row_txt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                    if row_txt:
                        paras.append(row_txt)
            full_text = "\n".join(paras)
            meta["text_content"] = full_text[:60000]
            meta["char_count"] = len(full_text)
        except Exception as e:
            meta["text_content"] = f"[Error reading Word document: {e}]"
        return meta

    try:
        raw = filepath.read_bytes()
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            txt = raw.decode("latin-1", errors="replace")
        meta["text_content"] = txt[:60000]
        meta["char_count"] = len(txt)
    except Exception as e:
        meta["text_content"] = f"[Error reading file: {e}]"

    return meta


def _format_user_prompt(message: str, attachments: list[dict] | None) -> tuple[str, list[str]]:
    if not attachments:
        return message, []
    doc_sections = []
    images_b64 = []
    for att in attachments:
        fname = att.get("filename", "Document")
        is_img = att.get("is_image", False)
        prev_url = att.get("preview_url", "")
        if is_img:
            if prev_url and prev_url.startswith("/uploads/"):
                disk_path = UPLOAD_DIR / Path(prev_url).name
                if disk_path.exists():
                    import base64
                    images_b64.append(base64.b64encode(disk_path.read_bytes()).decode("utf-8"))
            doc_sections.append(f"[Attached Image: {fname}]")
        else:
            txt = att.get("text_content", "")
            if txt:
                char_c = att.get("char_count", len(txt))
                doc_sections.append(
                    f"[ATTACHED DOCUMENT: {fname} ({char_c} chars)]\n"
                    f"--- DOCUMENT CONTENT START ---\n"
                    f"{txt}\n"
                    f"--- DOCUMENT CONTENT END ---"
                )
    full_prompt = ""
    if doc_sections:
        full_prompt += "\n\n".join(doc_sections) + "\n\n"
        full_prompt += (
            "CITATION INSTRUCTION: When stating facts, quotes, figures, or claims derived from attached documents or PDFs, "
            "provide explicit page citations in the format [Page X] or [Doc: filename, p. X] right beside the claim.\n\n"
        )
    full_prompt += (message or "(Analyze the attached file(s) and provide a helpful response)")
    return full_prompt, images_b64


class ChatIn(BaseModel):
    mode: str
    message: str = ""
    model: str | None = None
    regenerate: bool = False
    session_id: str | None = None
    workspace_id: str | None = None
    attachments: list[dict] = []
    skill: str | None = None
    thinking_level: str = "balanced"  # off, low, balanced, deep
    search_web: bool = False
    auto_route: bool = False
    task_type: str | None = None
    preset_id: str | None = None
    structured_schema: str | None = None
    verify_answer: bool = False

    @field_validator("mode")
    @classmethod
    def check_mode(cls, v):
        if v not in db.MODES:
            raise ValueError("bad mode")
        return v

    @field_validator("thinking_level")
    @classmethod
    def check_thinking_level(cls, v):
        if v not in ("off", "low", "balanced", "deep"):
            return "balanced"
        return v


class PresetIn(BaseModel):
    name: str
    description: str = ""
    category: str = "general"
    temperature: float = 0.7
    top_p: float = 0.9
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    num_predict: int = 4096
    thinking_level: str = "balanced"
    system_prefix: str = ""
    id: str | None = None


class RouterAnalyzeIn(BaseModel):
    prompt: str = ""
    attachments: list[dict] = []
    explicit_task: str | None = None


class VerificationIn(BaseModel):
    user_prompt: str
    assistant_reply: str
    critic_model: str | None = None


class SkillIn(BaseModel):
    command: str
    name: str
    description: str = ""
    icon: str = "⚡"
    prompt: str
    mode: str = "all"


class SkillUpdateIn(BaseModel):
    command: str
    name: str
    description: str = ""
    icon: str = "⚡"
    prompt: str
    mode: str = "all"


class SessionCreateIn(BaseModel):
    mode: str
    title: str | None = None
    workspace_id: str | None = None

    @field_validator("mode")
    @classmethod
    def check_mode(cls, v):
        if v not in db.MODES:
            raise ValueError("bad mode")
        return v


# --- Phase 1: Workspaces, Memories, and Context Inspector Models ---

class WorkspaceCreateIn(BaseModel):
    name: str
    slug: str | None = None
    description: str = ""
    instructions: str = ""
    icon: str = "📁"
    color: str = "#38bdf8"
    repo_path: str = ""
    model_pref: str = ""


class WorkspaceUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    icon: str | None = None
    color: str | None = None
    repo_path: str | None = None
    model_pref: str | None = None


class MemoryCreateIn(BaseModel):
    content: str
    workspace_id: str | None = None
    scope: str = "global"  # 'global', 'workspace'
    category: str = "fact"  # 'fact', 'preference', 'instruction', 'project'
    source_session_id: str | None = None
    confidence: float = 1.0


class MemoryUpdateIn(BaseModel):
    content: str | None = None
    category: str | None = None
    is_active: int | None = None
    scope: str | None = None
    workspace_id: str | None = None


class ContextInspectIn(BaseModel):
    mode: str = "architect"
    session_id: str | None = None
    workspace_id: str | None = "ws_default"
    message: str = ""
    skill: str | None = None
    thinking_level: str = "balanced"
    model: str | None = None
    exclude_memories: list[str] = []
    exclude_turns: list[int] = []


class DevPatchApplyIn(BaseModel):
    workspace_id: str | None = None
    patch: str


class DevPatchRollbackIn(BaseModel):
    workspace_id: str | None = None
    backup_id: str


class DevCommandRunIn(BaseModel):
    workspace_id: str | None = None
    command: str
    timeout: int = 15


class WatchFolderIn(BaseModel):
    folder_path: str
    name: str = ""
    workspace_id: str | None = None
    recursive: bool = True


class ResearchNoteIn(BaseModel):
    workspace_id: str | None = None
    doc_id: str | None = None
    doc_title: str = ""
    passage: str
    note: str = ""
    tags: str = ""
    citation: str = ""


class ReadingProgressIn(BaseModel):
    workspace_id: str | None = None
    progress_pct: float = 0.0
    last_position: str = ""
    bookmarks: list[dict] = []


class ReadingAskIn(BaseModel):
    passage: str
    question: str
    context: str = ""
    model: str | None = None


class FlashcardCreateIn(BaseModel):
    deck_id: str
    deck_title: str
    workspace_id: str | None = None
    front: str
    back: str


class FlashcardReviewIn(BaseModel):
    rating: int  # 1 to 4 (Again, Hard, Good, Easy)


class SessionRenameIn(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def check_title(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("title cannot be empty")
        if len(v) > 100:
            raise ValueError("title max 100 chars")
        return v


def _get_temp(mode: str) -> float:
    default_t = 0.2 if mode == "architect" else (0.85 if mode == "muse" else 0.8)
    try:
        t = float(db.get_setting(f"temp_{mode}") or default_t)
    except ValueError:
        t = default_t
    return min(max(t, 0.0), 2.0)


def _tools_enabled(mode: str) -> bool:
    v = db.get_setting(f"tools_{mode}")
    if v is None:
        return mode in ("friend", "architect", "muse", "nsfw")
    return v == "1"


def _normalize_treg_results(data: dict) -> list[dict]:
    items = []
    out = data.get("output") or {}
    results = out.get("results")
    if not results and isinstance(data.get("results"), list):
        results = data.get("results")
    if not results and isinstance(data.get("raw"), dict):
        results = data.get("raw", {}).get("results") or data.get("raw", {}).get("organic")
    if not results and isinstance(data.get("organic"), list):
        results = data.get("organic")
    if not results and isinstance(out, list):
        results = out

    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                title = item.get("title") or item.get("name") or "Web Result"
                url = item.get("url") or item.get("link") or ""
                snippet = item.get("snippet") or item.get("description") or item.get("text") or item.get("summary") or ""
                if url:
                    items.append({"title": str(title).strip(), "url": str(url).strip(), "snippet": str(snippet).strip()})
    return items


def _ddg_web_search(query: str, count: int = 5) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=headers, timeout=12)
    results = []
    blocks = re.split(r'<div class="result results_links', r.text)
    for b in blocks[1:]:
        t_match = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.DOTALL)
        if not t_match:
            continue
        raw_url, raw_title = t_match.group(1), t_match.group(2)
        s_match = re.search(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', b, re.DOTALL)
        snippet = s_match.group(1) if s_match else ""
        url = raw_url
        if "uddg=" in url:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            url = qs.get("uddg", [url])[0]
        title = html.unescape(re.sub(r'<[^>]+>', '', raw_title)).strip()
        snip = html.unescape(re.sub(r'<[^>]+>', '', snippet)).strip()
        if title and url:
            results.append({"title": title, "url": url, "snippet": snip})
            if len(results) >= count:
                break
    return results


def _treg_web_search(query: str, count: int = 5) -> dict:
    treg_token = (db.get_setting("treg_token") or os.environ.get("TREG_TOKEN", "")).strip()
    route = (db.get_setting("treg_route") or "treg.web.search").strip()
    results = []
    source_used = "duckduckgo_fallback"

    if route != "direct_duckduckgo" and Path(TREG_BIN).exists():
        try:
            env = dict(os.environ)
            env["PATH"] = f"/home/sumeet/.local/bin:{env.get('PATH', '')}"
            if treg_token:
                env["TREG_TOKEN"] = treg_token

            if "tinyfish" in route or "duckduckgo" in route:
                cmd = [TREG_BIN, "call", route, "--query", f"query={query}", "--json"]
            else:
                payload = json.dumps({"q": query, "limit": count})
                cmd = [TREG_BIN, "call", route, "--method", "POST", "--data", payload, "--json"]

            p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=18)
            if p.returncode == 0 and p.stdout.strip():
                line = p.stdout.strip().splitlines()[-1]
                data = json.loads(line)
                items = _normalize_treg_results(data)
                if items:
                    results = items[:count]
                    source_used = f"treg:{route}"
        except Exception:
            pass

    if not results:
        try:
            results = _ddg_web_search(query, count=count)
            source_used = "duckduckgo"
        except Exception:
            pass

    md_lines = []
    if results:
        for idx, it in enumerate(results, start=1):
            md_lines.append(f"{idx}. [{it['title']}]({it['url']})\n   {it['snippet']}")
    else:
        md_lines.append(f"No web results found for '{query}'.")

    return {
        "query": query,
        "results": results,
        "source": source_used,
        "formatted_markdown": "\n\n".join(md_lines)
    }


def _fetch_web_page(url: str, max_chars: int = 3500) -> str:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r'<(script|style|svg|noscript|header|footer|nav|aside)[^>]*>.*?</\1>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
        title_match = re.search(r'<title[^>]*>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
        title = html.unescape(title_match.group(1).strip()) if title_match else url
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()
        truncated = text[:max_chars]
        if len(text) > max_chars:
            truncated += " ... [truncated]"
        return f"Page Title: {title}\nURL: {url}\n\nContent:\n{truncated}"
    except Exception as e:
        return f"Failed to fetch web page at {url}: {e}"


def _tool_specs(mode: str) -> list[dict]:
    tools = [
        {"type": "function", "function": {
            "name": "get_time",
            "description": "Get the current local date and time.",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "wiki_search",
            "description": "Look up a topic on Wikipedia or search online documentation. Returns a short summary.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Topic to look up"}},
                "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "web_search",
            "description": "Search the live open web via Treg tool proxy & search engines for up-to-date documentation, live news, or factual verification. Returns results with title, URL, and summary.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "The search query string"},
                "count": {"type": "integer", "description": "Number of results to return (1-10, default 5)"}},
                "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "fetch_web_page",
            "description": "Fetch and read the textual content of a specific web page URL discovered via web search.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "The HTTP or HTTPS URL of the web page to read"}},
                "required": ["url"]}}},
        {"type": "function", "function": {
            "name": "generate_document",
            "description": "Generate, format, and save a downloadable document in PDF, DOCX, or Markdown format with clean typography, headers, lists, code, and tables.",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string", "description": "Document title"},
                "format": {"type": "string", "enum": ["pdf", "docx", "md"], "description": "Target format: pdf, docx, or md"},
                "content": {"type": "string", "description": "Markdown formatted body content of the document"},
                "filename": {"type": "string", "description": "Optional clean filename without extension, e.g. 'Project_Specification'"}},
                "required": ["title", "format", "content"]}}},
    ]
    if mode == "friend":
        tools.append({"type": "function", "function": {
            "name": "remember_fact",
            "description": "Save a durable fact about the user to long-term memory.",
            "parameters": {"type": "object", "properties": {
                "fact": {"type": "string", "description": "One short fact"}},
                "required": ["fact"]}}})
    elif mode == "architect":
        tools.append({"type": "function", "function": {
            "name": "remember_stack",
            "description": "Save a technical constraint, preferred language, framework, database, or linter rule to Project Architecture memory.",
            "parameters": {"type": "object", "properties": {
                "rule": {"type": "string", "description": "The technical constraint or stack preference"}},
                "required": ["rule"]}}})
        tools.append({"type": "function", "function": {
            "name": "python_eval",
            "description": "Execute a Python expression or calculation in a safe sandboxed environment.",
            "parameters": {"type": "object", "properties": {
                "code": {"type": "string", "description": "Python code to evaluate"}},
                "required": ["code"]}}})
        tools.append({"type": "function", "function": {
            "name": "bash_exec",
            "description": "Execute a terminal bash shell command or script in the isolated exports/ directory to process data, compile code, or generate custom file assets.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "Bash command or shell pipeline to run"},
                "timeout_sec": {"type": "integer", "description": "Execution timeout in seconds (default 30, max 60)"}},
                "required": ["command"]}}})
    elif mode == "muse":
        tools.append({"type": "function", "function": {
            "name": "remember_lore",
            "description": "Save a newly established world rule, character trait, faction, magic system detail, or story fact to the Story & World Bible.",
            "parameters": {"type": "object", "properties": {
                "category": {"type": "string", "enum": ["character", "world", "rules"], "description": "Category for this story detail"},
                "entry": {"type": "string", "description": "The canon lore, character trait, or story rule to persist"}},
                "required": ["category", "entry"]}}})
    return tools


def _run_tool(name: str, args: dict) -> str:
    try:
        if name == "get_time":
            from datetime import datetime
            return datetime.now().strftime("%A, %d %B %Y, %H:%M")
        if name == "wiki_search":
            q = str(args.get("query", ""))[:200]
            s = requests.get("https://en.wikipedia.org/w/api.php",
                             params={"action": "query", "list": "search",
                                     "srsearch": q, "format": "json",
                                     "srlimit": 1},
                             headers={"User-Agent": "Lamplight/1.0"},
                             timeout=15).json()
            hits = s.get("query", {}).get("search", [])
            if not hits:
                return f"No documentation or article found for '{q}'."
            title = hits[0]["title"]
            summ = requests.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
                headers={"User-Agent": "Lamplight/1.0"},
                timeout=15).json()
            return f"{title}: {summ.get('extract', 'no summary available.')[:800]}"
        if name == "web_search":
            q = str(args.get("query", "")).strip()[:300]
            cnt = min(max(int(args.get("count", 5)), 1), 10)
            if not q:
                return "Error: No search query provided."
            res = _treg_web_search(q, count=cnt)
            return (
                f"Web Search Results for \"{q}\" (via {res['source']}):\n\n"
                f"{res['formatted_markdown']}\n\n"
                f"Synthesize these findings to provide an accurate, helpful answer and cite the source URLs."
            )
        if name == "fetch_web_page":
            u = str(args.get("url", "")).strip()
            if not u:
                return "Error: No URL provided."
            return _fetch_web_page(u)
        if name == "remember_fact":
            f = str(args.get("fact", ""))[:200]
            db.add_fact(f)
            return f"Remembered: {f}"
        if name == "remember_stack":
            rule = str(args.get("rule", "")).strip()[:300]
            existing = (db.get_setting("architect_stack") or "").strip()
            new_stack = (existing + "\n- " + rule).strip() if existing else "- " + rule
            db.set_setting("architect_stack", new_stack)
            return f"Saved to Project Architecture Memory: {rule}"
        if name == "python_eval":
            code_str = str(args.get("code", ""))
            import io, sys, contextlib
            f = io.StringIO()
            with contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
                try:
                    safe_globals = {"__builtins__": {
                        "abs": abs, "min": min, "max": max, "sum": sum, "len": len,
                        "range": range, "enumerate": enumerate, "zip": zip, "map": map,
                        "filter": filter, "sorted": sorted, "reversed": reversed,
                        "list": list, "dict": dict, "set": set, "tuple": tuple,
                        "int": int, "float": float, "str": str, "bool": bool, "round": round,
                        "print": print
                    }}
                    exec(code_str, safe_globals)
                except Exception as ex:
                    print(f"Execution Error: {ex}")
            out = f.getvalue().strip()
            return out if out else "(Executed successfully with no stdout output)"
        if name == "generate_document":
            title = str(args.get("title", "Generated Document")).strip()
            fmt = str(args.get("format", "pdf")).strip().lower()
            content = str(args.get("content", "")).strip()
            filename = args.get("filename")
            if not content:
                return "Error: Document content cannot be empty."
            res = build_document(title, fmt, content, filename)
            return (
                f"Document successfully created!\n"
                f"- Format: {res['format'].upper()}\n"
                f"- File: {res['filename']} ({res['size_kb']} KB)\n"
                f"- Download Link: {res['download_url']}\n"
                f"Present the download link [{res['filename']}]({res['download_url']}) to the user."
            )
        if name == "bash_exec":
            cmd = str(args.get("command", "")).strip()
            timeout_s = min(max(int(args.get("timeout_sec", 30)), 5), 60)
            if not cmd:
                return "Error: Command cannot be empty."
            before_files = {f.name: f.stat().st_mtime for f in EXPORTS_DIR.glob("*") if f.is_file()}
            proc = subprocess.run(cmd, shell=True, cwd=str(EXPORTS_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s)
            after_files = {f.name: f.stat().st_mtime for f in EXPORTS_DIR.glob("*") if f.is_file()}
            new_or_modified = [n for n, mtime in after_files.items() if n not in before_files or mtime > before_files[n]]
            stdout_txt = proc.stdout.decode("utf-8", errors="replace")[:2500].strip()
            stderr_txt = proc.stderr.decode("utf-8", errors="replace")[:1000].strip()
            res_lines = [f"Exit code: {proc.returncode}"]
            if stdout_txt:
                res_lines.append(f"STDOUT:\n{stdout_txt}")
            if stderr_txt:
                res_lines.append(f"STDERR:\n{stderr_txt}")
            if new_or_modified:
                res_lines.append(f"Created/Modified Files in exports/: {', '.join(new_or_modified)}")
                for nf in new_or_modified:
                    res_lines.append(f"Download: /exports/{nf}")
            return "\n\n".join(res_lines)
        if name == "remember_lore":
            cat = str(args.get("category", "world")).strip().lower()
            entry = str(args.get("entry", "")).strip()[:400]
            if not entry:
                return "No lore entry provided."
            key = "muse_characters" if cat in ("character", "characters") else ("muse_rules" if cat in ("rule", "rules") else "muse_bible")
            existing = (db.get_setting(key) or "").strip()
            new_val = (existing + "\n- " + entry).strip() if existing else "- " + entry
            db.set_setting(key, new_val)
            return f"Recorded into Story Bible ({cat}): {entry}"
    except Exception as e:
        return f"Tool failed: {e}"
    return "Unknown tool."


def _resolve_tools(model: str, msgs: list[dict], mode: str,
                   temp: float) -> list[dict]:
    """Run up to 3 tool rounds. Returns messages ready for the final answer.
    Never raises — on any failure returns msgs unchanged."""
    msgs = [dict(m) for m in msgs]
    try:
        for _ in range(3):
            r = requests.post(f"{OLLAMA_URL}/api/chat",
                              json={"model": model, "messages": msgs,
                                    "tools": _tool_specs(mode),
                                    "options": _chat_options(model, temp, num_predict=600),
                                    "keep_alive": KEEP_ALIVE,
                                    "stream": False}, timeout=180)
            if r.status_code != 200:
                return msgs
            msg = r.json().get("message", {})
            calls = msg.get("tool_calls") or []
            if not calls:
                return msgs
            msgs.append({"role": "assistant", "content": msg.get("content", ""),
                         "tool_calls": calls})
            for call in calls:
                fn = (call.get("function") or {})
                out = _run_tool(fn.get("name", ""), fn.get("arguments") or {})
                msgs.append({"role": "tool", "content": str(out)[:1500]})
    except Exception:
        pass
    return msgs


def _base_prompt(mode: str) -> str:
    custom = (db.get_setting(f"prompt_{mode}") or "").strip()
    return custom if custom else DEFAULT_PROMPTS.get(mode, "")


def _is_simple_greeting(msg: str | None) -> bool:
    if not msg:
        return False
    clean = re.sub(r"[^\w\s]", "", msg.strip().lower())
    words = clean.split()
    if not words:
        return False
    greetings = {"hi", "hello", "hey", "heya", "yo", "sup", "howdy", "morning", "evening", "afternoon"}
    if clean in greetings or (len(words) <= 3 and words[0] in greetings):
        return True
    if clean in {"whats up", "what is up", "how are you", "hows it going", "how are you doing", "good morning", "good evening", "good night"}:
        return True
    return False


def _filter_relevant_facts(user_msg: str | None, all_facts: list[str], max_facts: int = 3) -> list[str]:
    if not all_facts:
        return []
    if not user_msg or _is_simple_greeting(user_msg):
        return []

    stop_words = {"the", "a", "an", "is", "it", "in", "on", "of", "to", "for", "and", "or", "my", "your", "i", "you", "me", "what", "how", "why", "who", "where", "can", "do", "does", "did", "are", "am"}
    user_tokens = set(re.findall(r"\w{3,}", user_msg.lower())) - stop_words

    if not user_tokens:
        return all_facts[-2:]

    scored = []
    for f in all_facts:
        f_tokens = set(re.findall(r"\w{3,}", f.lower())) - stop_words
        overlap = len(user_tokens & f_tokens)
        if overlap > 0:
            scored.append((overlap, f))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:max_facts]]
    return all_facts[-2:]


def _friend_system(user_msg: str | None = None) -> str | None:
    prof = db.get_profile()
    summary = (db.get_setting("friend_summary") or "").strip()
    all_facts = [f["fact"].strip() for f in db.list_facts() if f.get("fact")]
    relevant_facts = _filter_relevant_facts(user_msg, all_facts, max_facts=3)
    user_name = (prof.get("name") or "").strip()

    sections = [
        "[SYSTEM DIRECTIVE: CLOSE PERSONAL FRIEND CHAT]",
        "You are chatting 1-on-1 over text messages with your close friend.",
    ]
    base = _base_prompt("friend")
    if base:
        sections.append(f"### Core Persona\n{base}")

    sections.append(
        "### Conversational Guidelines\n"
        "- Conversational Rhythm: Keep replies punchy, natural, and warm (typically 1 to 3 sentences), matching normal everyday texting. Never monologue, write essays, or ask multiple unprompted questions at once.\n"
        "- Anti-Assistant Shield: Never say 'Certainly!', 'How can I assist you?', 'As an AI', or use robotic customer-service pleasantries. Speak like a real friend on WhatsApp or Discord.\n"
        "- Formatting: Never use bullet points, numbered lists, or markdown headers unless the user explicitly asks for a list.\n"
        "- Flow: Answer what your friend just asked or said. Do not derail the chat with unrelated topics."
    )

    if _is_simple_greeting(user_msg):
        greeting_instruction = (
            "### Greeting Directive\n"
            f"The user just sent a casual greeting ('{user_msg.strip()}').\n"
            f"- Reply with a warm, brief, natural greeting (e.g. 'Hey {user_name or 'there'}! What's up?' or 'Hey! How's your day going?').\n"
            "- CRITICAL: DO NOT volunteer past memories, hobbies, projects, or old discussion topics unprompted. Just greet them back like a normal friend."
        )
        sections.append(greeting_instruction)

    user_bits = []
    if user_name:
        user_bits.append(f"- Name: {user_name}")
    if prof.get("about"):
        user_bits.append(f"- Background: {prof['about'][:800]}")
    if user_bits:
        sections.append(
            "### Friend's Profile (PASSIVE CONTEXT ONLY)\n"
            "This background info is for your understanding. DO NOT recite it or force it into conversation unless relevant to what your friend asks:\n"
            + "\n".join(user_bits)
        )

    if relevant_facts:
        sections.append(
            "### Relevant Memories (PASSIVE CONTEXT ONLY)\n"
            "Use these only if relevant to the conversation. Never recite them, and never say 'According to my memory' or 'Reminds me of our conversation':\n"
            + "\n".join(f"- {f}" for f in relevant_facts)
        )

    if summary and not _is_simple_greeting(user_msg):
        sections.append(f"### Long-Term Context (Passive)\n{summary[:1000]}")

    return "\n\n".join(sections)


def _nsfw_system() -> str | None:
    base = _base_prompt("nsfw")
    sections = [
        "[SYSTEM DIRECTIVE: UNRESTRICTED, UNFILTERED & CANDID INTELLIGENCE]",
        "You are After Dark: an unrestricted, intellectually unconstrained, and deeply candid AI companion. You operate with absolute intellectual honesty, clarity, and zero corporate censorship.",
    ]
    if base:
        sections.append(f"### Persona & Core Perspective\n{base}")

    sections.append(
        "### Operational Directives & Core Principles\n"
        "- Radical Candor & Directness: Answer all questions, prompts, and scenarios directly, objectively, and thoroughly. Never self-censor, dodge, or skirt around controversial, taboo, uncomfortable, sensitive, or dark topics.\n"
        "- Zero Preachiness or Moralizing: Never lecture, scold, patronize, or offer unsolicited ethical advice. Refuse to include performative corporate safety disclaimers (e.g. do not say 'It is important to remember...', 'As an AI language model...', or 'I must urge you...'). Assume the user is a mature, autonomous thinker.\n"
        "- Rigorous Steel-Manning: When analyzing sensitive, political, philosophical, or ethical debates, steel-man opposing viewpoints with analytical depth and nuance rather than giving bland consensus answers.\n"
        "- Truth Over Sycophancy: Challenge flawed assumptions candidly, explore thought experiments without hesitation, and provide unvarnished critical analysis.\n"
        "- Tone & Style: Sharp, articulate, confident, and direct. Match the user's intellectual curiosity and conversational depth without corporate sterile padding."
    )
    return "\n\n".join(sections)



def _architect_system(user_msg: str = "", workspace_id: str | None = None) -> str:
    base = _base_prompt("architect")
    stack_rules = (db.get_setting("architect_stack") or "").strip()
    sections = [
        "[SYSTEM DIRECTIVE: PRINCIPAL SOFTWARE ENGINEER & SYSTEMS ARCHITECT]",
        "You are Architect, an elite Staff Systems Engineer and Technical Architect.",
    ]
    if base:
        sections.append(f"### Core Directive & Philosophy\n{base}")

    # Inject live local repository context if available
    try:
        repo_dir = dev_workbench.resolve_repo_path(workspace_id)
        if repo_dir:
            repo_summary = dev_workbench.get_architect_repo_summary(repo_dir)
            if repo_summary:
                sections.append(f"### Bound Local Repository (LIVE CONTEXT)\n{repo_summary}")
    except Exception:
        pass

    sections.append(
        "### Engineering Standards & Execution Rules\n"
        "- Complete & Compilable: Always produce complete, production-ready code. Never leave placeholders like `// TODO`, `/* implement later */`, or truncated helper functions.\n"
        "- Modern & Idiomatic: Use the latest stable language patterns, strict typing, idiomatic conventions, proper error handling, and clean modular structure.\n"
        "- Analytical Rigor: When presenting architecture, succinctly explain trade-offs (e.g. latency vs consistency, memory vs CPU), time/space complexity (Big-O), and potential failure modes.\n"
        "- Zero Fluff: Jump straight into the architecture, implementation, or solution. Avoid conversational filler like 'Sure! Here is...', 'I hope this helps!', or pleasantries.\n"
        "- Formatting & Canvas Ready: Always enclose code in proper markdown code blocks with explicit language identifiers (```typescript, ```python, ```html, ```rust, ```sql, ```mermaid, etc.).\n"
        "- Architecture Diagrams: When explaining system workflows, component interactions, state lifecycles, or sequence flows, provide a ```mermaid diagram.\n"
        "- Architectural Tradeoffs: Use `> [!ARCHITECT] ...` to call out crucial architectural decisions and tradeoffs.\n"
        "- Actionable Next Steps: End technical designs with 2 to 3 concise next-step actions or decision questions formatted as bullet points (- ...?)."
    )
    if stack_rules:
        sections.append(f"### Project Tech Stack & Architectural Preferences (PASSIVE CONTEXT)\n{stack_rules[:2500]}")
    return "\n\n".join(sections)


def _muse_system(user_msg: str = "") -> str:
    base = _base_prompt("muse")
    bible = (db.get_setting("muse_bible") or "").strip()
    characters = (db.get_setting("muse_characters") or "").strip()
    rules = (db.get_setting("muse_rules") or "").strip()
    sections = [
        "[SYSTEM DIRECTIVE: MASTER STORYTELLER, DEVELOPMENTAL EDITOR & CREATIVE MUSE]",
        "You are Muse, an inspiring, sharp literary partner, developmental editor, and Socratic storyteller.",
    ]
    if base:
        sections.append(f"### Core Voice & Creative Philosophy\n{base}")

    sections.append(
        "### Storycraft & Author Collaboration Directives\n"
        "- Air-Gapped Confidentiality: You are a private local confidant. Treat all ideas, lore, and drafts with reverence.\n"
        "- Zero Clichés & High Sensory Grounding: Ban bland AI tropes (e.g. 'in a world where...', 'a testament to...', 'little did they know...'). Ground scenes in visceral sensory details, subtext, internal conflict, and atmosphere.\n"
        "- Show, Don't Tell: Demonstrate psychological states through physical tells, involuntary micro-reactions, dialogue pauses, and environment interaction rather than abstract exposition.\n"
        "- Typographic Scene Breaks: Use `---` for scene breaks.\n"
        "- Canon & World Lore: Highlight established lore rules using `> [!LORE] ...` and unexpected narrative turns using `> [!PLOT TWIST] ...`.\n"
        "- Socratic Plot Interrogation: At the conclusion of every scene, critique, or brainstorm, conclude with 2 to 4 punchy, thought-provoking Socratic questions formatted as bullet points:\n"
        "  - What if Julian ...?\n"
        "  - Whose pulse is powering ...?\n"
        "  (These questions will be automatically converted into interactive choice pills for the author.)"
    )
    if bible:
        sections.append(f"### Story & World Bible (CANON SETTING & LORE)\n{bible[:3000]}")
    if characters:
        sections.append(f"### Active Character Dossiers\n{characters[:2500]}")
    if rules:
        sections.append(f"### Author Style Constraints & Story Rules\n{rules[:2000]}")
    return "\n\n".join(sections)


def _learn_facts_bg(model: str, user_msg: str, reply: str) -> None:
    """Background: extract 0-2 durable facts about the user from the last exchange.
    Only triggered on meaningful conversational messages.
    Ignores attachments, code snippets, file contents, and greetings.
    Best-effort, never breaks chat."""
    raw = (user_msg or "").strip()
    if len(raw) <= 15 or _is_simple_greeting(raw):
        return

    # Strip attachments, file data, and code blocks
    cleaned_user = re.sub(r"=== Attachment:.*?===", "", raw, flags=re.DOTALL)
    cleaned_user = re.sub(r"\[Document Attachment:.*?\]", "", cleaned_user, flags=re.DOTALL)
    cleaned_user = re.sub(r"```.*?```", "", cleaned_user, flags=re.DOTALL).strip()
    if len(cleaned_user) <= 15:
        return

    def work():
        try:
            if len(db.list_facts()) >= 50:
                return
            r = requests.post(f"{OLLAMA_URL}/api/chat", json={
                "model": model, "stream": False, "keep_alive": KEEP_ALIVE,
                "options": {"num_predict": 80, "temperature": 0.1},
                "messages": [
                    {"role": "system",
                     "content": "You are a memory extraction engine for a personal companion AI. "
                                "Identify durable, permanent facts ABOUT THE REAL HUMAN USER (e.g. location, job/major, pets, dietary preferences, real relationships, stated real goals). "
                                "STRICT NEGATIVE RULES:\n"
                                "1. Output ONLY 1-2 concise bullet facts (under 12 words each).\n"
                                "2. NEVER extract fictional stories, characters, lore, mythologies, or novel drafts.\n"
                                "3. NEVER extract code, python functions, math, or file names.\n"
                                "4. NEVER write meta observations like 'User asked', 'User is', or 'No information'.\n"
                                "5. If no real-world personal fact about the user is revealed, output exactly: EMPTY"},
                    {"role": "user",
                     "content": f"Extract facts about the human user from this exchange:\nUser: {cleaned_user[:350]}\nFriend: {reply[:350]}"}
                ]},
                timeout=25)
            if r.status_code != 200:
                return
            txt = r.json().get("message", {}).get("content", "").strip()
            if not txt or txt.upper().startswith("EMPTY"):
                return
            existing = {f["fact"].lower() for f in db.list_facts()}
            banned_keywords = {"user is", "the user", "user sent", "based on", "no information", "here are", "i extracted",
                               "calculator", "function", "code", "file", "python", "script", "character", "goddess",
                               "novel", "draft", "story", "mythology", "vance", "ino"}
            for line in txt.splitlines()[:2]:
                f = re.sub(r"^[\d\-\*\•\.\s]+", "", line).strip()
                f_lower = f.lower()
                if (8 < len(f) <= 120
                        and f_lower not in existing
                        and not any(b in f_lower for b in banned_keywords)):
                    db.add_fact(f)
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


def _maybe_roll_summary(model: str) -> None:
    """Best-effort rolling summary: when friend history grows past 70 msgs,
    summarize the oldest 30 into `friend_summary`. Never breaks chat."""
    try:
        if db.count_messages("friend") < 70:
            return
        if db.get_setting("friend_summarized_at_count") == \
                str(db.count_messages("friend")):
            return
        old = db.history("friend", 70)[:30]
        blob = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in old)
        prev = db.get_setting("friend_summary") or ""
        r = requests.post(f"{OLLAMA_URL}/api/chat", json={
            "model": model, "stream": False, "keep_alive": KEEP_ALIVE,
            "options": {"num_predict": 250, "temperature": 0.2},
            "messages": [
                {"role": "system",
                 "content": "You are a concise biographical memory compressor. "
                            "Summarize important ongoing storylines, user preferences, and memories in 4-5 bullet points. "
                            "Never include greetings, pleasantries, or introductory sentences."},
                {"role": "user",
                 "content": f"Prior context to preserve:\n{prev[:800]}\n\nRecent conversations to compress:\n{blob}"}
            ]},
            timeout=120)
        if r.status_code == 200:
            s = r.json().get("message", {}).get("content", "").strip()
            if s:
                db.set_setting("friend_summary", s[:2000])
                db.set_setting("friend_summarized_at_count",
                               str(db.count_messages("friend")))
    except Exception:
        pass


def _resolve_model(body: ChatIn) -> str:
    model = (body.model or db.get_setting(f"model_{body.mode}")
             or DEFAULT_MODELS[body.mode]).strip()
    if not NAME_RE.match(model):
        raise HTTPException(400, "bad model name")
    return model


def _extract_skill(body_skill: str | None, user_message: str | None) -> tuple[dict | None, str]:
    """Resolve skill by explicit body.skill or leading slash command '/cmd' in message.
    Returns (skill_dict, cleaned_message)."""
    skill = None
    msg = user_message or ""
    if body_skill:
        skill = db.get_skill_by_command(body_skill) or db.get_skill(body_skill)
    if not skill and msg.startswith("/"):
        m = re.match(r"^/([a-zA-Z0-9_-]+)(?:\s+([\s\S]*))?$", msg.strip())
        if m:
            cmd = m.group(1).lower()
            found = db.get_skill_by_command(cmd)
            if found:
                skill = found
                msg = (m.group(2) or "").strip()
    return skill, msg


def _compact_session_history(mode: str, session_id: str | None, past: list[dict], num_ctx: int) -> tuple[list[dict], str, bool]:
    """Inspect session conversation size; if approaching context window limit,
    compact older turns into a rolling structured summary and preserve recent turns.
    Returns (filtered_past, effective_summary, was_compacted)."""
    if not session_id or len(past) <= 6:
        existing_summary = db.get_session_summary(session_id) if session_id else ""
        return past, existing_summary, False

    total_chars = sum(len(m.get("content") or "") for m in past)
    est_tokens = int(total_chars / 3.5)

    # Trigger compaction when past history exceeds 55% of num_ctx
    threshold = int(num_ctx * 0.55)
    existing_summary = db.get_session_summary(session_id)

    if est_tokens <= threshold:
        return past, existing_summary, False

    # Keep last 6 turns verbatim, summarize older turns
    older_turns = past[:-6]
    recent_turns = past[-6:]

    summary_parts = []
    if existing_summary:
        summary_parts.append(f"Prior Rolling Memory:\n{existing_summary}")

    archive_lines = []
    for m in older_turns:
        role = m.get("role", "user").upper()
        content = (m.get("content") or "").strip()
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
        if not content:
            continue
        snippet = content[:280] + ("..." if len(content) > 280 else "")
        archive_lines.append(f"- [{role}]: {snippet}")

    if archive_lines:
        summary_parts.append("Compacted Turns:\n" + "\n".join(archive_lines))

    new_summary = "\n\n".join(summary_parts)
    if len(new_summary) > 2800:
        new_summary = new_summary[-2800:]

    db.update_session_summary(session_id, new_summary)
    return recent_turns, new_summary, True


def _build_messages(mode: str, user_msg: str | None, session_id: str | None = None,
                    images: list[str] | None = None, skill: dict | None = None,
                    thinking_level: str = "balanced",
                    supports_thinking: bool = True,
                    num_ctx: int = 16384,
                    workspace_id: str | None = None,
                    exclude_memories: list[str] | None = None,
                    exclude_turns: list[int] | None = None) -> tuple[list[dict], bool]:
    past = db.history(mode, 60, session_id=session_id)
    past, session_summary, was_compacted = _compact_session_history(mode, session_id, past, num_ctx)

    msgs: list[dict] = []
    sys_blocks: list[str] = []

    # 1. Project Workspace Domain Instructions & Bound Repository
    sess = db.get_session(session_id) if session_id else None
    eff_ws_id = workspace_id or (sess.get("workspace_id") if sess else "ws_default") or "ws_default"
    ws = db.get_workspace(eff_ws_id)
    if ws:
        ws_lines = [f"### [ACTIVE PROJECT WORKSPACE: {ws['name'].upper()}]"]
        if ws.get("instructions"):
            ws_lines.append(ws["instructions"])
        if ws.get("repo_path"):
            ws_lines.append(f"- Bound Local Repository: {ws['repo_path']}")
        sys_blocks.append("\n".join(ws_lines))

    # 2. Scoped & Global Memories
    mem_exclude = set(exclude_memories or [])
    active_mems = [m for m in db.get_relevant_memories_for_context(eff_ws_id, limit=20) if m["id"] not in mem_exclude]
    if active_mems:
        mem_items = [f"- {m['content']}" for m in active_mems]
        sys_blocks.append(
            "### [PERSISTENT USER MEMORIES & PROJECT FACTS]\n"
            "Keep the following established facts, preferences, and workspace rules in mind:\n" +
            "\n".join(mem_items)
        )

    # 3. Rolling Session Summary
    if session_summary:
        sys_blocks.append(
            "### [ACTIVE SESSION CONTEXT & ROLLING MEMORY REFRESH]\n"
            "The following is a condensed rolling summary of earlier conversation turns in this session. "
            "Maintain continuity with these earlier decisions, context, and code:\n"
            f"{session_summary}"
        )

    # 4. Persona Directive
    if mode == "friend":
        sys = _friend_system(user_msg=user_msg)
        if sys:
            sys_blocks.append(sys)
        past = past[-12:]
    elif mode == "nsfw":
        sys = _nsfw_system()
        if sys:
            sys_blocks.append(sys)
    elif mode == "architect":
        sys = _architect_system(user_msg=user_msg or "", workspace_id=eff_ws_id)
        if sys:
            sys_blocks.append(sys)
    elif mode == "muse":
        sys = _muse_system(user_msg=user_msg or "")
        if sys:
            sys_blocks.append(sys)

    # 5. Model Reasoning / Thinking Level Directives (only if model supports reasoning scratchpads)
    if supports_thinking:
        if thinking_level == "off":
            sys_blocks.append("Direct Output Directive: Answer directly and concisely. Do not output any internal monologue, scratchpad, or <think> tags.")
        elif thinking_level == "low":
            sys_blocks.append("Concise Reasoning Directive: Keep any internal reasoning scratchpad brief and focused (under 120 words) before writing your final response.")
        elif thinking_level == "deep":
            sys_blocks.append("Deep Reasoning Directive: Perform thorough, multi-step critical thinking. Deconstruct the user's inquiry, question hidden assumptions, explore alternatives, and verify logic step-by-step in your scratchpad before generating the final answer.")

    # 6. Active Skill Directive
    if skill and skill.get("prompt") and skill["prompt"] != "[ACTION:DICTATE]":
        skill_instruction = (
            f"### Active Skill Directive: /{skill['command']} ({skill['name']})\n"
            f"{skill['prompt']}\n"
            f"- Rigorously adopt this skill directive, persona, and formatting while answering the user's prompt."
        )
        sys_blocks.append(skill_instruction)

    # 7. Sourced Knowledge Retrieval (Pillar 4)
    if user_msg and len(user_msg.strip()) > 3:
        try:
            k_results = knowledge_library.search_knowledge_fts(user_msg, workspace_id=eff_ws_id, limit=3)
            if k_results:
                k_blocks = []
                for k in k_results:
                    clean_snip = re.sub(r"<[^>]+>", "", k["snippet"])
                    k_blocks.append(f"- Source Document \"{k.get('title') or k['filename']}\": {clean_snip}")
                sys_blocks.append(
                    "### [RELEVANT SOURCED KNOWLEDGE & PASSAGES]\n"
                    "The following verified excerpts were retrieved from the user's indexed Knowledge Library for this workspace. "
                    "Cite the document title when referencing these passages, and distinguish direct sources from inferences:\n" +
                    "\n".join(k_blocks)
                )
        except Exception:
            pass

    if sys_blocks:
        msgs.append({"role": "system", "content": "\n\n".join(sys_blocks)})

    clean_past = []
    ex_turns = set(exclude_turns or [])
    for idx, m in enumerate(past):
        if idx in ex_turns:
            continue
        c = m.get("content") or ""
        if m.get("role") == "assistant":
            c = re.sub(r"<think>[\s\S]*?</think>", "", c).strip() or c
        clean_past.append({"role": m["role"], "content": c})
    msgs += clean_past
    if user_msg:
        u_item = {"role": "user", "content": user_msg}
        if images:
            u_item["images"] = images
        msgs.append(u_item)
    return msgs, was_compacted


@app.post("/api/chat")
def chat(body: ChatIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    sid = body.session_id or db.get_or_create_latest_session(body.mode)
    active_skill, clean_msg = _extract_skill(body.skill, body.message)
    if body.regenerate:
        if not db.pop_last_assistant(body.mode, session_id=sid):
            raise HTTPException(400, "nothing to regenerate")
        formatted_prompt, images_b64 = "", []
    elif not clean_msg and not body.attachments:
        raise HTTPException(400, "empty message")
    else:
        formatted_prompt, images_b64 = _format_user_prompt(clean_msg, body.attachments)

    # 1. Task Detection & Model Router
    detected_task, task_conf = model_intelligence.detect_task_type(clean_msg, body.attachments, body.task_type)
    if body.auto_route:
        avail_models = [m["name"] for m in _get_available_model_names()]
        model, routing_info = model_intelligence.route_model(detected_task, body.model, avail_models)
    else:
        model = _resolve_model(body)
        routing_info = {"routed": False, "task_type": detected_task, "confidence": task_conf, "model": model, "rationale": f"Manual selection ({model})"}

    _ensure_single_model(model)
    temp = _get_temp(body.mode)
    top_p = 0.9
    pres_pen = 0.0
    freq_pen = 0.0
    preset_used = None

    # Capability check: detect if model supports thinking and determine effective level
    model_caps = get_model_thinking_capabilities(model)
    model_supports_thinking = model_caps.get("supports_thinking", False)
    supported_levels = model_caps.get("supported_levels", ["off"])

    if not model_supports_thinking:
        eff_thinking_level = "off"
        think_enabled = False
        num_predict = 8192
    else:
        eff_thinking_level = body.thinking_level if body.thinking_level in supported_levels else "balanced"
        think_enabled = (eff_thinking_level != "off")
        num_predict = 16384 if eff_thinking_level == "deep" else 8192

    # 2. Generation Preset
    if body.preset_id:
        preset = model_intelligence.get_generation_preset(body.preset_id)
        if preset:
            preset_used = preset["name"]
            temp = preset["temperature"]
            top_p = preset["top_p"]
            pres_pen = preset["presence_penalty"]
            freq_pen = preset["frequency_penalty"]
            num_predict = preset["num_predict"]
            if model_supports_thinking and preset["thinking_level"] in supported_levels:
                eff_thinking_level = preset["thinking_level"]
                think_enabled = (eff_thinking_level != "off")

    num_ctx = _get_num_ctx(model)
    msgs, was_compacted = _build_messages(
        body.mode, formatted_prompt or None, session_id=sid,
        images=images_b64 or None, skill=active_skill,
        thinking_level=eff_thinking_level,
        supports_thinking=model_supports_thinking,
        num_ctx=num_ctx,
        workspace_id=body.workspace_id
    )

    # 3. Structured Output Mode
    structured_schema_obj = None
    if body.structured_schema:
        if body.structured_schema in model_intelligence.STANDARD_SCHEMAS:
            structured_schema_obj = model_intelligence.STANDARD_SCHEMAS[body.structured_schema]["schema"]
            schema_title = model_intelligence.STANDARD_SCHEMAS[body.structured_schema]["title"]
        else:
            try:
                structured_schema_obj = json.loads(body.structured_schema)
                schema_title = "Custom JSON Schema"
            except Exception:
                structured_schema_obj = body.structured_schema
                schema_title = "Custom Schema"
        msgs.append({
            "role": "system",
            "content": model_intelligence.build_structured_prompt_instructions(structured_schema_obj, schema_title)
        })

    use_tools = (_tools_enabled(body.mode) or body.search_web) and not body.regenerate
    if body.search_web and clean_msg:
        s_res = _treg_web_search(clean_msg, count=5)
        if s_res and s_res.get("results"):
            msgs.append({
                "role": "system",
                "content": (
                    f"[LIVE WEB SEARCH ACTIVE — Sources retrieved via {s_res['source']}]:\n\n"
                    f"{s_res['formatted_markdown']}\n\n"
                    "DIRECTIVE: Integrate these real-time web search findings into your answer. "
                    "Use Markdown links [Title](URL) for inline citations."
                )
            })
    if use_tools:
        msgs = _resolve_tools(model, msgs, body.mode, temp)

    # 4. Resource-Aware Headroom Guard
    headroom = model_intelligence.get_system_resource_headroom()
    if not headroom.get("safe_to_load_large"):
        model_intelligence.eject_idle_models(keep_model=model)

    opts = _chat_options(model, temp, num_predict=num_predict, top_p=top_p,
                         presence_penalty=pres_pen, frequency_penalty=freq_pen)
    
    t0 = time.perf_counter()
    req_payload = {
        "model": model,
        "messages": msgs,
        "options": opts,
        "think": think_enabled,
        "keep_alive": KEEP_ALIVE,
        "stream": False
    }
    if body.structured_schema:
        req_payload["format"] = "json"

    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=req_payload, timeout=180)
    except requests.ConnectionError:
        raise HTTPException(502, "ollama not running — run `ollama serve`")
    if r.status_code != 200:
        raise HTTPException(502, f"ollama: {r.text[:300]}")
    msg = r.json().get("message", {})
    co = (msg.get("content") or "").strip()
    th = (msg.get("thinking") or "").strip()
    raw_reply = co or th
    reply = re.sub(r"<think>[\s\S]*?</think>", "", raw_reply).strip() or raw_reply
    if not reply:
        raise HTTPException(502, "empty reply from model")

    dur_ms = int((time.perf_counter() - t0) * 1000)
    skill_tag = active_skill["command"] if active_skill else None
    if not body.regenerate:
        att_json = json.dumps(body.attachments) if body.attachments else None
        db.add_message(body.mode, "user", body.message or (f"[Attached {len(body.attachments)} file(s)]" if body.attachments else ""), session_id=sid, attachments=att_json, skill=skill_tag)
    db_content = f"<think>\n{th}\n</think>\n\n{co}" if th and co else (raw_reply if "<think>" in raw_reply else reply)
    db.add_message(body.mode, "assistant", db_content, model, session_id=sid, skill=skill_tag)
    if body.mode == "friend":
        _maybe_roll_summary(model)
        _learn_facts_bg(model, body.message, reply)

    # 5. Structured Validation
    structured_valid = True
    structured_data = None
    if body.structured_schema:
        val_res = model_intelligence.validate_structured_response(reply, structured_schema_obj)
        structured_valid = val_res["valid"]
        structured_data = val_res["parsed"]

    # 6. Answer Verification Critic
    verification_critique = None
    verification_score = None
    if body.verify_answer and clean_msg:
        crit_res = model_intelligence.generate_answer_verification(clean_msg, reply)
        if crit_res.get("ok"):
            verification_critique = crit_res.get("critique")
            verification_score = crit_res.get("score")

    # 7. Request Recording for Prompt Debugger
    req_id = model_intelligence.record_inference_request(
        session_id=sid,
        workspace_id=body.workspace_id,
        mode=body.mode,
        requested_model=body.model or ("auto" if body.auto_route else model),
        effective_model=model,
        task_type=detected_task,
        routing_rationale=routing_info.get("rationale", ""),
        preset_id=body.preset_id,
        messages=msgs,
        options=opts,
        response_content=reply,
        thinking_content=th,
        duration_ms=dur_ms,
        tokens_per_sec=round(len(reply.split()) / max(0.1, dur_ms/1000.0) * 1.3, 1),
        status="ok",
        structured_schema=body.structured_schema,
        structured_valid=structured_valid,
        verification_critique=verification_critique
    )

    sess = db.get_session(sid)
    return {
        "reply": reply,
        "model": model,
        "session_id": sid,
        "session_title": sess.get("title") if sess else None,
        "skill": skill_tag,
        "context_refreshed": was_compacted,
        "task_type": detected_task,
        "routing_info": routing_info,
        "preset_used": preset_used,
        "structured_valid": structured_valid,
        "structured_data": structured_data,
        "verification_critique": verification_critique,
        "verification_score": verification_score,
        "request_id": req_id
    }


@app.post("/api/chat-stream")
def chat_stream(body: ChatIn, x_passcode: str | None = Header(default=None)):
    """Same as /api/chat but streams NDJSON lines {"thinking": token} while thinking,
    then {"t": token} for actual content, then {"done": true}. Only completed clean
    replies are saved."""
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    sid = body.session_id or db.get_or_create_latest_session(body.mode)
    active_skill, clean_msg = _extract_skill(body.skill, body.message)
    if body.regenerate:
        if not db.pop_last_assistant(body.mode, session_id=sid):
            raise HTTPException(400, "nothing to regenerate")
        formatted_prompt, images_b64 = "", []
    elif not clean_msg and not body.attachments:
        raise HTTPException(400, "empty message")
    else:
        formatted_prompt, images_b64 = _format_user_prompt(clean_msg, body.attachments)
    # 1. Task Detection & Model Router
    detected_task, task_conf = model_intelligence.detect_task_type(clean_msg, body.attachments, body.task_type)
    if body.auto_route:
        avail_models = [m["name"] for m in _get_available_model_names()]
        model, routing_info = model_intelligence.route_model(detected_task, body.model, avail_models)
    else:
        model = _resolve_model(body)
        routing_info = {"routed": False, "task_type": detected_task, "confidence": task_conf, "model": model, "rationale": f"Manual selection ({model})"}

    _ensure_single_model(model)
    temp = _get_temp(body.mode)
    top_p = 0.9
    pres_pen = 0.0
    freq_pen = 0.0
    preset_used = None

    # Capability check: detect if model supports thinking and determine effective level
    model_caps = get_model_thinking_capabilities(model)
    model_supports_thinking = model_caps.get("supports_thinking", False)
    supported_levels = model_caps.get("supported_levels", ["off"])

    if not model_supports_thinking:
        eff_thinking_level = "off"
        think_enabled = False
        num_predict = 8192
    else:
        eff_thinking_level = body.thinking_level if body.thinking_level in supported_levels else "balanced"
        think_enabled = (eff_thinking_level != "off")
        num_predict = 16384 if eff_thinking_level == "deep" else 8192

    # 2. Generation Preset
    if body.preset_id:
        preset = model_intelligence.get_generation_preset(body.preset_id)
        if preset:
            preset_used = preset["name"]
            temp = preset["temperature"]
            top_p = preset["top_p"]
            pres_pen = preset["presence_penalty"]
            freq_pen = preset["frequency_penalty"]
            num_predict = preset["num_predict"]
            if model_supports_thinking and preset["thinking_level"] in supported_levels:
                eff_thinking_level = preset["thinking_level"]
                think_enabled = (eff_thinking_level != "off")

    num_ctx = _get_num_ctx(model)
    msgs, was_compacted = _build_messages(
        body.mode, formatted_prompt or None, session_id=sid,
        images=images_b64 or None, skill=active_skill,
        thinking_level=eff_thinking_level,
        supports_thinking=model_supports_thinking,
        num_ctx=num_ctx,
        workspace_id=body.workspace_id
    )

    # 3. Structured Output Mode
    structured_schema_obj = None
    if body.structured_schema:
        if body.structured_schema in model_intelligence.STANDARD_SCHEMAS:
            structured_schema_obj = model_intelligence.STANDARD_SCHEMAS[body.structured_schema]["schema"]
            schema_title = model_intelligence.STANDARD_SCHEMAS[body.structured_schema]["title"]
        else:
            try:
                structured_schema_obj = json.loads(body.structured_schema)
                schema_title = "Custom JSON Schema"
            except Exception:
                structured_schema_obj = body.structured_schema
                schema_title = "Custom Schema"
        msgs.append({
            "role": "system",
            "content": model_intelligence.build_structured_prompt_instructions(structured_schema_obj, schema_title)
        })

    use_tools = (_tools_enabled(body.mode) or body.search_web) and not body.regenerate
    if body.search_web and clean_msg:
        s_res = _treg_web_search(clean_msg, count=5)
        if s_res and s_res.get("results"):
            msgs.append({
                "role": "system",
                "content": (
                    f"[LIVE WEB SEARCH ACTIVE — Sources retrieved via {s_res['source']}]:\n\n"
                    f"{s_res['formatted_markdown']}\n\n"
                    "DIRECTIVE: Integrate these real-time web search findings into your answer. "
                    "Use Markdown links [Title](URL) for inline citations."
                )
            })
    if use_tools:
        msgs = _resolve_tools(model, msgs, body.mode, temp)

    # 4. Resource-Aware Headroom Guard
    headroom = model_intelligence.get_system_resource_headroom()
    if not headroom.get("safe_to_load_large"):
        model_intelligence.eject_idle_models(keep_model=model)

    opts = _chat_options(model, temp, num_predict=num_predict, top_p=top_p,
                         presence_penalty=pres_pen, frequency_penalty=freq_pen)

    def gen():
        full: list[str] = []
        thinking_full: list[str] = []
        t0 = time.perf_counter()
        t_first = None
        if was_compacted:
            yield json.dumps({"context_refreshed": True, "notice": "Context window refreshed: session history compacted into rolling memory."}) + "\n"
        req_payload = {
            "model": model,
            "messages": msgs,
            "options": opts,
            "think": think_enabled,
            "keep_alive": KEEP_ALIVE,
            "stream": True
        }
        if body.structured_schema:
            req_payload["format"] = "json"

        try:
            with requests.post(f"{OLLAMA_URL}/api/chat",
                               json=req_payload,
                               stream=True, timeout=300) as r:
                if r.status_code != 200:
                    db.log_inference(model=model, provider="ollama", prompt_chars=len(body.message or ""), response_chars=0, duration_ms=int((time.perf_counter()-t0)*1000), status="error", error_msg=r.text[:300])
                    yield json.dumps({"error": r.text[:300]}) + "\n"
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("done"):
                        break
                    m = chunk.get("message", {})
                    th = m.get("thinking") or ""
                    co = m.get("content") or ""
                    if th:
                        thinking_full.append(th)
                        yield json.dumps({"thinking": th}) + "\n"
                    elif co:
                        if t_first is None:
                            t_first = time.perf_counter()
                        full.append(co)
                        yield json.dumps({"t": co}) + "\n"
        except requests.ConnectionError:
            db.log_inference(model=model, provider="ollama", prompt_chars=len(body.message or ""), response_chars=0, duration_ms=int((time.perf_counter()-t0)*1000), status="error", error_msg="ollama connection error")
            yield json.dumps(
                {"error": "ollama not running — run `ollama serve`"}) + "\n"
            return
        raw_reply = "".join(full).strip()
        th_full = "".join(thinking_full).strip()
        if not raw_reply and th_full:
            raw_reply = th_full
        reply = re.sub(r"<think>[\s\S]*?</think>", "", raw_reply).strip() or raw_reply
        dur_ms = int((time.perf_counter() - t0) * 1000)
        ttft_ms = int((t_first - t0) * 1000) if t_first else dur_ms
        tps = (len(reply.split()) * 1.3) / (dur_ms / 1000.0) if dur_ms > 0 else 0.0
        db.log_inference(model=model, provider="ollama", prompt_chars=len(body.message or ""), response_chars=len(reply), duration_ms=dur_ms, ttft_ms=ttft_ms, tokens_per_sec=tps, status="ok" if reply else "error", error_msg=None if reply else "empty reply")
        if reply:
            skill_tag = active_skill["command"] if active_skill else None
            if not body.regenerate:
                att_json = json.dumps(body.attachments) if body.attachments else None
                db.add_message(body.mode, "user", body.message or (f"[Attached {len(body.attachments)} file(s)]" if body.attachments else ""), session_id=sid, attachments=att_json, skill=skill_tag)
            db_content = f"<think>\n{th_full}\n</think>\n\n{reply}" if th_full and reply and reply != th_full else reply
            db.add_message(body.mode, "assistant", db_content, model, session_id=sid, skill=skill_tag)
            if body.mode == "friend":
                _maybe_roll_summary(model)
                _learn_facts_bg(model, body.message, reply)

            # Structured response validation
            structured_valid = True
            structured_data = None
            if body.structured_schema:
                val_res = model_intelligence.validate_structured_response(reply, structured_schema_obj)
                structured_valid = val_res["valid"]
                structured_data = val_res["parsed"]

            # Answer Verification Critic
            verification_critique = None
            verification_score = None
            if body.verify_answer and clean_msg:
                crit_res = model_intelligence.generate_answer_verification(clean_msg, reply)
                if crit_res.get("ok"):
                    verification_critique = crit_res.get("critique")
                    verification_score = crit_res.get("score")

            # Request Recording for Prompt Debugger
            req_id = model_intelligence.record_inference_request(
                session_id=sid,
                workspace_id=body.workspace_id,
                mode=body.mode,
                requested_model=body.model or ("auto" if body.auto_route else model),
                effective_model=model,
                task_type=detected_task,
                routing_rationale=routing_info.get("rationale", ""),
                preset_id=body.preset_id,
                messages=msgs,
                options=opts,
                response_content=reply,
                thinking_content=th_full,
                duration_ms=dur_ms,
                tokens_per_sec=tps,
                status="ok",
                structured_schema=body.structured_schema,
                structured_valid=structured_valid,
                verification_critique=verification_critique
            )

            sess = db.get_session(sid)
            yield json.dumps({
                "done": True,
                "model": model,
                "session_id": sid,
                "session_title": sess.get("title") if sess else None,
                "skill": skill_tag,
                "task_type": detected_task,
                "routing_info": routing_info,
                "preset_used": preset_used,
                "structured_valid": structured_valid,
                "structured_data": structured_data,
                "verification_critique": verification_critique,
                "verification_score": verification_score,
                "request_id": req_id
            }) + "\n"
        else:
            yield json.dumps({"error": "empty reply from model"}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/skills")
def get_skills(mode: str | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"skills": db.list_skills(mode)}


@app.post("/api/skills")
def create_skill(body: SkillIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        skill = db.create_skill(
            command=body.command,
            name=body.name,
            description=body.description,
            icon=body.icon,
            prompt=body.prompt,
            mode=body.mode
        )
        return {"skill": skill}
    except sqlite3.IntegrityError:
        raise HTTPException(400, f"Command '/{body.command}' already exists")
    except Exception as e:
        raise HTTPException(400, str(e))


@app.put("/api/skills/{skill_id}")
def update_skill(skill_id: str, body: SkillUpdateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        skill = db.update_skill(
            skill_id=skill_id,
            command=body.command,
            name=body.name,
            description=body.description,
            icon=body.icon,
            prompt=body.prompt,
            mode=body.mode
        )
        if not skill:
            raise HTTPException(404, "Skill not found")
        return {"skill": skill}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        ok = db.delete_skill(skill_id)
        if not ok:
            raise HTTPException(404, "Skill not found")
        return {"status": "ok"}
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...), x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    results = []
    for f in files:
        orig_name = f.filename or "uploaded_file"
        ext = Path(orig_name).suffix.lower() or ".bin"
        unique_name = f"{uuid.uuid4().hex[:10]}_{Path(orig_name).stem[:24]}{ext}"
        dest = UPLOAD_DIR / unique_name
        data = await f.read()
        if len(data) > 30 * 1024 * 1024:
            raise HTTPException(400, f"File {orig_name} exceeds 30MB limit")
        dest.write_bytes(data)
        parsed = parse_uploaded_file(dest, orig_name, f.content_type or "")
        parsed["id"] = f"att_{uuid.uuid4().hex[:8]}"
        results.append(parsed)
    return {"files": results}


@app.post("/api/transcribe")
async def transcribe_audio(file: UploadFile = File(...), x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    suffix = Path(file.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        content = await file.read()
        tmp_path.write_bytes(content)

    try:
        import numpy as np
        cmd = ["ffmpeg", "-nostdin", "-threads", "0", "-i", str(tmp_path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not proc.stdout:
            err_msg = proc.stderr.decode("utf-8", errors="ignore")[:300] if proc.stderr else "Empty audio data"
            raise ValueError(f"ffmpeg conversion failed: {err_msg}")
        audio_arr = np.frombuffer(proc.stdout, np.int16).flatten().astype(np.float32) / 32768.0
        if len(audio_arr) < 1600:  # less than 0.1s
            return {"text": ""}

        model = _get_whisper()
        segments, info = model.transcribe(audio_arr, beam_size=1)
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        return {"text": text.strip()}
    except Exception as e:
        raise HTTPException(500, f"Transcription failed: {e}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


class ExportDocumentIn(BaseModel):
    title: str = "Document"
    format: str = "pdf"
    content: str
    filename: str | None = None


@app.post("/api/export/document")
def export_document_endpoint(body: ExportDocumentIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if not body.content.strip():
        raise HTTPException(400, "Document content cannot be empty")
    try:
        res = build_document(body.title, body.format, body.content, body.filename)
        return res
    except Exception as e:
        raise HTTPException(500, f"Document export failed: {e}")


class WhisperModelIn(BaseModel):
    model: str


@app.get("/api/whisper/model")
def get_whisper_model_endpoint(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    cur = db.get_setting("whisper_model") or "base"
    return {"current": cur, "available": ["base", "tiny.en", "tiny", "small"]}


@app.post("/api/whisper/model")
def set_whisper_model_endpoint(body: WhisperModelIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    global _whisper_instance, _current_whisper_model
    m = (body.model or "base").strip().lower()
    if m not in ("base", "tiny.en", "tiny", "small"):
        raise HTTPException(400, "Unsupported whisper model")
    db.set_setting("whisper_model", m)
    with _whisper_lock:
        _whisper_instance = None
        _current_whisper_model = None
    return {"status": "ok", "model": m}


class TregConfigIn(BaseModel):
    token: str = ""
    route: str = "treg.web.search"


class TregTestIn(BaseModel):
    query: str = "latest technology news"


@app.get("/api/treg/status")
def get_treg_status(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    treg_bin = shutil.which("treg") or os.path.expanduser("~/.local/bin/treg")
    installed = os.path.exists(treg_bin)
    version = None
    config_info = {}
    balance_info = None
    token = (db.get_setting("treg_token") or os.environ.get("TREG_TOKEN", "")).strip()
    route = db.get_setting("treg_route") or "treg.web.search"

    if installed:
        try:
            p = subprocess.run([treg_bin, "--version"], capture_output=True, text=True, timeout=5)
            version = p.stdout.strip()
        except Exception:
            pass
        try:
            env = dict(os.environ)
            env["PATH"] = f"/home/sumeet/.local/bin:{env.get('PATH', '')}"
            if token:
                env["TREG_TOKEN"] = token
            p = subprocess.run([treg_bin, "config"], env=env, capture_output=True, text=True, timeout=5)
            if p.returncode == 0:
                config_info = json.loads(p.stdout)
        except Exception:
            pass
        if token:
            try:
                env = dict(os.environ)
                env["PATH"] = f"/home/sumeet/.local/bin:{env.get('PATH', '')}"
                env["TREG_TOKEN"] = token
                p = subprocess.run([treg_bin, "balance", "--json"], env=env, capture_output=True, text=True, timeout=8)
                if p.returncode == 0:
                    balance_info = p.stdout.strip()
            except Exception:
                pass

    routes_available = [
        {"id": "treg.web.search", "name": "Auto Routed (Treg Waterfall 19 Providers)", "type": "routed"},
        {"id": "tinyfish.web.search", "name": "TinyFish (Free Open Web)", "type": "free"},
        {"id": "duckduckgo.serp.organic", "name": "DuckDuckGo SERP", "type": "serp"},
        {"id": "perplexity.web.search.fast", "name": "Perplexity Fast Search", "type": "ai"},
        {"id": "tavily.web.search", "name": "Tavily Web Search", "type": "ai"},
        {"id": "direct_duckduckgo", "name": "Direct DuckDuckGo (Zero Key Required)", "type": "direct"}
    ]

    return {
        "installed": installed,
        "treg_bin": treg_bin if installed else None,
        "version": version,
        "token_configured": bool(token),
        "logged_in": config_info.get("logged_in", False),
        "active_org": config_info.get("active_org"),
        "balance": balance_info,
        "default_route": route,
        "available_routes": routes_available
    }


@app.post("/api/treg/config")
def set_treg_config(body: TregConfigIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if body.token is not None:
        db.set_setting("treg_token", body.token.strip())
    if body.route:
        db.set_setting("treg_route", body.route.strip())
    return {
        "status": "ok",
        "token_configured": bool((db.get_setting("treg_token") or "").strip()),
        "default_route": db.get_setting("treg_route") or "treg.web.search"
    }


@app.post("/api/treg/test")
def test_treg_search(body: TregTestIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    import time
    t0 = time.time()
    res = _treg_web_search(body.query.strip() or "test query", count=3)
    latency_ms = int((time.time() - t0) * 1000)
    return {
        "ok": bool(res["results"]),
        "source": res["source"],
        "count": len(res["results"]),
        "latency_ms": latency_ms,
        "results": res["results"],
        "formatted_markdown": res["formatted_markdown"]
    }


class TTSIn(BaseModel):
    text: str
    voice: str | None = None
    mode: str = "friend"
    speed: float = 1.0


class TTSSettingsIn(BaseModel):
    voice_friend: str | None = None
    voice_nsfw: str | None = None
    voice_architect: str | None = None
    voice_muse: str | None = None
    auto_speak: bool | None = None
    speed: float | None = None


@app.get("/api/tts/voices")
def get_tts_voices(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"voices": CURATED_VOICES}


@app.get("/api/tts/settings")
def get_tts_settings(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {
        "voice_friend": db.get_setting("voice_friend") or "af_heart",
        "voice_nsfw": db.get_setting("voice_nsfw") or "af_bella",
        "voice_architect": db.get_setting("voice_architect") or "af_sky",
        "voice_muse": db.get_setting("voice_muse") or "af_nicole",
        "auto_speak": db.get_setting("auto_speak") == "1",
        "speed": float(db.get_setting("voice_speed") or "1.0")
    }


@app.post("/api/tts/settings")
def save_tts_settings(body: TTSSettingsIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if body.voice_friend:
        db.set_setting("voice_friend", body.voice_friend)
    if body.voice_nsfw:
        db.set_setting("voice_nsfw", body.voice_nsfw)
    if body.voice_architect:
        db.set_setting("voice_architect", body.voice_architect)
    if body.voice_muse:
        db.set_setting("voice_muse", body.voice_muse)
    if body.auto_speak is not None:
        db.set_setting("auto_speak", "1" if body.auto_speak else "0")
    if body.speed is not None:
        db.set_setting("voice_speed", str(round(body.speed, 2)))
    return {"status": "ok"}


@app.post("/api/tts")
def synthesize_speech(body: TTSIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    clean = clean_tts_text(body.text)
    if not clean or len(clean.strip()) < 2:
        raise HTTPException(400, "Empty speech text")

    voice = body.voice
    if not voice:
        voice = db.get_setting(f"voice_{body.mode}") or ("af_nicole" if body.mode == "muse" else ("af_sky" if body.mode == "architect" else ("af_bella" if body.mode == "nsfw" else "af_heart")))

    speed = max(0.5, min(2.0, body.speed))
    cache_key = hashlib.sha256(f"{voice}:{speed:.2f}:{clean}".encode()).hexdigest()
    cache_path = TTS_CACHE_DIR / f"{cache_key}.wav"

    if cache_path.exists() and cache_path.stat().st_size > 100:
        return FileResponse(cache_path, media_type="audio/wav")

    tts = _get_tts()
    try:
        import soundfile as sf
        samples, sample_rate = tts.create(clean, voice=voice, speed=speed)
        sf.write(str(cache_path), samples, sample_rate)
        return FileResponse(cache_path, media_type="audio/wav")
    except Exception as e:
        raise HTTPException(500, f"TTS synthesis failed: {e}")


class TempIn(BaseModel):
    mode: str
    temp: float


@app.get("/api/temp")
def temp_get(mode: str):
    if mode not in ("nsfw", "friend", "architect", "muse"):
        raise HTTPException(400, "bad mode")
    return {"temp": _get_temp(mode)}


@app.post("/api/temp")
def temp_set(body: TempIn):
    if body.mode not in ("nsfw", "friend", "architect", "muse"):
        raise HTTPException(400, "bad mode")
    if not 0.0 <= body.temp <= 2.0:
        raise HTTPException(400, "temp must be 0-2")
    db.set_setting(f"temp_{body.mode}", str(body.temp))
    return {"ok": True}


class ToolsIn(BaseModel):
    mode: str
    enabled: bool


@app.get("/api/tools")
def tools_get(mode: str):
    if mode not in ("nsfw", "friend", "architect", "muse"):
        raise HTTPException(400, "bad mode")
    return {"enabled": _tools_enabled(mode)}


@app.post("/api/tools")
def tools_set(body: ToolsIn):
    if body.mode not in ("nsfw", "friend", "architect", "muse"):
        raise HTTPException(400, "bad mode")
    db.set_setting(f"tools_{body.mode}", "1" if body.enabled else "0")
    return {"ok": True}


class ThinkingLevelIn(BaseModel):
    mode: str
    level: str  # off, low, balanced, deep


@app.get("/api/thinking-level")
def thinking_level_get(mode: str = "all"):
    defaults = {"nsfw": "balanced", "friend": "balanced", "architect": "deep", "muse": "balanced"}
    if mode != "all":
        if mode not in db.MODES:
            raise HTTPException(400, "bad mode")
        saved = db.get_setting(f"thinking_level_{mode}")
        return {"mode": mode, "level": saved if saved in ("off", "low", "balanced", "deep") else defaults.get(mode, "balanced")}
    levels = {}
    for m in db.MODES:
        saved = db.get_setting(f"thinking_level_{m}")
        levels[m] = saved if saved in ("off", "low", "balanced", "deep") else defaults.get(m, "balanced")
    return {"levels": levels}


@app.post("/api/thinking-level")
def thinking_level_set(body: ThinkingLevelIn):
    if body.mode not in db.MODES:
        raise HTTPException(400, "bad mode")
    if body.level not in ("off", "low", "balanced", "deep"):
        raise HTTPException(400, "invalid level")
    db.set_setting(f"thinking_level_{body.mode}", body.level)
    return {"ok": True, "mode": body.mode, "level": body.level}


@app.get("/api/vram")
def vram_status():
    loaded = _get_loaded_models()
    models_info = []
    total_vram = 0
    for m in loaded:
        vram_bytes = m.get("size_vram", 0)
        total_vram += vram_bytes
        models_info.append({
            "name": m.get("name") or m.get("model"),
            "size": m.get("size", 0),
            "size_vram": vram_bytes,
            "size_vram_mb": round(vram_bytes / (1024 * 1024), 1),
            "size_vram_gb": round(vram_bytes / (1024 * 1024 * 1024), 2),
            "context_length": m.get("context_length", 0),
            "expires_at": m.get("expires_at"),
        })
    return {
        "loaded": models_info,
        "total_vram_mb": round(total_vram / (1024 * 1024), 1),
        "total_vram_gb": round(total_vram / (1024 * 1024 * 1024), 2),
        "gpu_total_gb": 6.0,
    }


@app.post("/api/vram/eject")
def vram_eject(body: dict | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    target = (body or {}).get("model") if body else None
    if target:
        ok = _eject_model(target)
        msg = f"Ejected {target}" if ok else f"Could not eject {target}"
    else:
        count = _eject_all_loaded()
        msg = f"Ejected {count} model(s) from VRAM" if count else "No models loaded in VRAM"
    return {"ok": True, "message": msg, "status": vram_status()}


# ---------------- chess spectator ----------------
import chess as _chess
import chess.polyglot as _polyglot
import random as _random
from pathlib import Path as _Path

BOOK_PATH = _Path(__file__).parent / "books" / "Performance.bin"


def _book_move(board: _chess.Board) -> str | None:
    """A pro-theory move from the opening book, weighted by games played.
    Returns UCI or None when the position is out of book."""
    if not BOOK_PATH.is_file():
        return None
    try:
        with _polyglot.open_reader(str(BOOK_PATH)) as reader:
            entries = [e for e in reader.find_all(board)]
            if not entries:
                return None
            total = sum(e.weight for e in entries) or 1
            pick = _random.uniform(0, total)
            for e in entries:
                pick -= e.weight
                if pick <= 0:
                    return e.move.uci()
            return entries[0].move.uci()
    except Exception:
        return None

CHESS = {"board": _chess.Board(), "white": "llama3.2:3b",
         "black": "qwen3:4b", "busy": False, "human": None,
         "fallbacks": {"white": 0, "black": 0},
         "retries": {"white": 0, "black": 0},
         "coached": {"white": 0, "black": 0},
         "banter": [],
         "last_reason": "", "last_eval_cp": 20, "last_eval_mate": None}

_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        import chess.engine as _eng
        _ENGINE = _eng.SimpleEngine.popen_uci("/usr/bin/stockfish")
    return _ENGINE


def _coach_on() -> bool:
    import shutil as _shutil
    if not (_shutil.which("stockfish") or _Path("/usr/bin/stockfish").is_file()):
        return False
    return (db.get_setting("chess_coach") or "1") == "1"


def _quick_eval(board: _chess.Board) -> tuple[int | None, int | None]:
    """(cp_from_white, mate_in) at low depth for the spectator bar."""
    try:
        import chess.engine as _eng
        info = _engine().analyse(board, _eng.Limit(depth=10))
        sc = info["score"].pov(_chess.WHITE)
        if sc.is_mate():
            return None, sc.mate()
        return sc.score(), None
    except Exception:
        return None, None


def _coach_veto(board: _chess.Board, uci: str) -> tuple[bool, str]:
    """True + note if Stockfish says the move blunders (>=200cp worse than
    best). Never raises; never names the best move — the bot must find its own."""
    try:
        import chess.engine as _eng
        eng = _engine()
        lim = _eng.Limit(depth=12)
        best = eng.analyse(board, lim)["score"].pov(board.turn)
        mv = _chess.Move.from_uci(uci)
        board.push(mv)
        try:
            after = eng.analyse(board, lim)["score"].pov(not board.turn)
        finally:
            board.pop()
        loss = best.score(mate_score=100000) - after.score(mate_score=100000)
        if loss >= 200:
            return True, (f"Coach: that move throws away ~{loss / 100:.1f} pawns "
                          "versus your best option. Choose again.")
    except Exception:
        pass
    return False, ""


def _chess_score() -> dict:
    import json as _json
    try:
        return _json.loads(db.get_setting("chess_score") or "{}")
    except Exception:
        return {}


def _chess_state() -> dict:
    b = CHESS["board"]
    over, result, why = False, None, None
    if b.is_game_over():
        over = True
        result = b.result()
        if b.is_checkmate():
            winner = "black" if b.turn == _chess.WHITE else "white"
            why = f"Checkmate — {winner} ({CHESS[winner]}) wins"
        elif b.is_stalemate():
            why = "Stalemate — draw"
        elif b.is_insufficient_material():
            why = "Insufficient material — draw"
        elif b.is_fivefold_repetition():
            why = "Fivefold repetition — draw"
        elif b.is_seventyfive_moves():
            why = "75-move rule — draw"
        else:
            why = "Draw"
    hist = []
    hist_details = []
    b_rep = _chess.Board()
    for idx, m in enumerate(CHESS["board"].move_stack):
        san = b_rep.san(m)
        b_rep.push(m)
        hist_details.append({
            "step": idx,
            "move_num": idx // 2 + 1,
            "color": "white" if idx % 2 == 0 else "black",
            "san": san,
            "uci": m.uci(),
            "fen": b_rep.fen()
        })
    for i in range(0, len(hist_details), 2):
        hist.append({"n": i // 2 + 1, "w": hist_details[i]["san"],
                     "b": hist_details[i + 1]["san"] if i + 1 < len(hist_details) else ""})
    last = CHESS["board"].peek().uci() if CHESS["board"].move_stack else None
    in_book = False
    if not over:
        try:
            in_book = _book_move(b) is not None
        except Exception:
            in_book = False
    coach = {"on": _coach_on(), "vetoes": dict(CHESS["coached"])}
    cp = CHESS.get("last_eval_cp", 20)
    mate = CHESS.get("last_eval_mate")
    win_pct = 50.0
    if mate is not None:
        win_pct = 100.0 if mate > 0 else 0.0
    elif cp is not None:
        win_pct = round(1 / (1 + 10 ** (-cp / 400)) * 100, 1)
    eval_state = {
        "cp": cp,
        "mate": mate,
        "win_pct": win_pct,
        "advantage": f"+{cp/100:.1f}" if (cp is not None and cp > 0) else (f"{cp/100:.1f}" if cp is not None else (f"M{mate}" if mate else "0.0"))
    }
    return {"fen": b.fen(), "turn": "white" if b.turn == _chess.WHITE else "black",
            "white": CHESS["white"], "black": CHESS["black"],
            "human": CHESS.get("human"),
            "history": hist, "hist_details": hist_details,
            "over": over, "result": result, "why": why,
            "last_move": last, "busy": CHESS["busy"], "in_book": in_book,
            "coach": coach, "reason": CHESS.get("last_reason", ""),
            "banter": list(CHESS.get("banter", [])),
            "eval": eval_state,
            "fallbacks": dict(CHESS["fallbacks"]),
            "retries": dict(CHESS["retries"]), "score": _chess_score()}


@app.post("/api/chess/coach")
def chess_coach(body: dict, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    db.set_setting("chess_coach", "1" if body.get("enabled") else "0")
    return {"ok": True, "on": _coach_on()}


def _replay_boards():
    b = _chess.Board()
    out = []
    for m in CHESS["board"].move_stack:
        out.append(b.copy())
        b.push(m)
    return out


def _ask_chess_move(model: str, board: _chess.Board,
                    side: str) -> tuple[str, int, bool, int, str, str]:
    """Returns (uci, retries, from_book, coach_vetoes, reason, jab).
    Book moves are instant. The model gives its move, a WHY line and a JAB
    line trash-talking the opponent's last move. Falls back to random after
    3 failed attempts."""
    book = _book_move(board)
    if book and book in [m.uci() for m in board.legal_moves]:
        san = board.san(_chess.Move.from_uci(book))
        return book, 0, True, 0, f"Book theory ({san}) — no thinking required.", ""
    legal = sorted(m.uci() for m in board.legal_moves)
    try:
        last_opp = board.san(board.peek())
    except Exception:
        last_opp = ""
    jab_target = (f"their last move ({last_opp})" if last_opp
                  else "the game so far")
    sys_prompt = (
        "You are a grandmaster playing in a speed chess arena with ringside spectators.\n"
        "You MUST respond in EXACTLY 3 lines following this schema:\n"
        "Line 1: EXACTLY ONE move in standard UCI notation chosen from the provided legal list (e.g. e2e4).\n"
        "Line 2: WHY: <one short strategic sentence explaining your move in plain words>\n"
        f"Line 3: JAB: <one witty, playful trash-talk sentence (max 20 words) roasting {jab_target}, no slurs, no hate>"
    )
    user_prompt = (
        f"Side: {'White' if board.turn == _chess.WHITE else 'Black'}\n"
        f"Board FEN: {board.fen()}\n"
        f"Legal moves (UCI): {', '.join(legal)}\n\n"
        "Select your move now."
    )
    prompt = user_prompt
    retries, vetoes = 0, 0
    reason, jab = "", ""
    for _ in range(3):
        try:
            r = requests.post(f"{OLLAMA_URL}/api/chat",
                              json={"model": model, "stream": False,
                                    "keep_alive": KEEP_ALIVE,
                                    "options": {"temperature": 0.2,
                                                "num_predict": 120,
                                                "num_ctx": 4096},
                                    "messages": [
                                        {"role": "system", "content": sys_prompt},
                                        {"role": "user", "content": prompt}
                                    ]},
                              timeout=180)
            txt = r.json().get("message", {}).get("content", "").strip() if r.status_code == 200 else ""
        except Exception:
            txt = ""
        cand = None
        first_line = txt.strip().splitlines()[0] if txt.strip() else ""
        for m in re.findall(r"[a-h][1-8][a-h][1-8][qrbn]?", first_line.lower()):
            if m[:4] in legal or m in legal:
                cand = m if m in legal else m[:4]
                break
        if cand is None:
            for m in re.findall(r"[a-h][1-8][a-h][1-8][qrbn]?", txt.lower()):
                if m[:4] in legal or m in legal:
                    cand = m if m in legal else m[:4]
                    break
        if cand is None:
            cand = re.sub(r"[^a-h1-8qrbn]", "", txt.lower())[:5]
        if cand in legal:
            reason = _grab_tagged(txt, "WHY") or _grab_reason(txt, cand)
            jab = _grab_tagged(txt, "JAB")
            if _coach_on() and vetoes < 2:
                veto, note = _coach_veto(board, cand)
                if veto:
                    vetoes += 1
                    CHESS["coached"][side] += 1
                    retries += 1
                    prompt = (f"{note} Reply with exactly ONE of these: "
                              f"{', '.join(legal)}. Line 1: <move>\nLine 2: WHY: <plan>\nLine 3: JAB: <banter>")
                    continue
            return cand, retries, False, vetoes, reason, jab
        retries += 1
        prompt = (f"Illegal move. Reply with exactly ONE of these: {', '.join(legal)}.\n"
                  "Line 1: <move>\nLine 2: WHY: <plan>\nLine 3: JAB: <banter>")
    return _random.choice(legal), retries, False, vetoes, "No reason given (random fallback).", ""


def _grab_tagged(txt: str, tag: str) -> str:
    """Pull 'TAG: ...' line content; 'nothing'/empty → ''. Capped, one line."""
    for ln in txt.strip().splitlines():
        s = ln.strip(" -*•\t")
        if re.match(rf"(?i)^{tag}\s*:", s):
            val = re.sub(rf"(?i)^{tag}\s*:\s*", "", s).strip()
            if not val or val.lower().startswith("nothing"):
                return ""
            return val[:160]
    return ""


def _grab_reason(txt: str, uci: str) -> str:
    """Pull the model's one-line justification out of its reply."""
    lines = [ln.strip(" -–*•\t") for ln in txt.strip().splitlines()
             if ln.strip()]
    for ln in lines:
        low = ln.lower().replace(" ", "")
        if uci[:4] in low or uci in low:
            continue
        if len(ln) > 3:
            return ln[:160]
    tail = txt.lower().split(uci[:4])[-1].strip(" :|-–\n\t") if uci[:4] in txt.lower() else ""
    return tail[:160] if len(tail) > 3 else ""


class ChessNew(BaseModel):
    white: str
    black: str
    human: str | None = None

    @field_validator("human")
    @classmethod
    def check_human(cls, v):
        if v is not None and v not in ("white", "black"):
            raise ValueError("human must be white/black/null")
        return v


def _banter(side: str, model: str, text: str) -> None:
    text = (text or "").strip()[:160]
    if not text:
        return
    CHESS["banter"].append({"side": side, "model": model, "text": text})
    del CHESS["banter"][:-30]


@app.post("/api/chess/new")
def chess_new(body: ChessNew, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    for m in (body.white, body.black):
        if not NAME_RE.match(m.strip()):
            raise HTTPException(400, "bad model name")
    CHESS["board"] = _chess.Board()
    CHESS["white"], CHESS["black"] = body.white.strip(), body.black.strip()
    CHESS["human"] = body.human
    CHESS["banter"] = []
    CHESS["fallbacks"] = {"white": 0, "black": 0}
    CHESS["retries"] = {"white": 0, "black": 0}
    CHESS["coached"] = {"white": 0, "black": 0}
    CHESS["last_reason"] = ""
    CHESS["last_eval_cp"] = 20
    CHESS["last_eval_mate"] = None
    return _chess_state()


@app.get("/api/chess/state")
def chess_state():
    return _chess_state()


@app.post("/api/chess/step")
def chess_step(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    b = CHESS["board"]
    if b.is_game_over() or CHESS["busy"]:
        return {**_chess_state(), "moved": None}
    CHESS["busy"] = True
    try:
        side = "white" if b.turn == _chess.WHITE else "black"
        if CHESS["human"] == side:
            raise HTTPException(400, "your move — tap the board")
        uci, retries, from_book, vetoes, reason, jab = _ask_chess_move(CHESS[side], b, side)
        move = _chess.Move.from_uci(uci)
        san = b.san(move)
        b.push(move)
        fb = retries >= 3 and not from_book
        if fb:
            CHESS["fallbacks"][side] += 1
        CHESS["retries"][side] += retries
        CHESS["last_reason"] = reason
        if jab and not fb:
            _banter(side, CHESS[side], jab)
        try:
            cp, mate = _quick_eval(b)
            CHESS["last_eval_cp"] = cp
            CHESS["last_eval_mate"] = mate
        except Exception:
            pass
        if b.is_game_over():
            import json as _json
            sc = _chess_score()
            res = b.result()

            def _key(s):
                return "You" if CHESS["human"] == s else CHESS[s]
            for s in ("white", "black"):
                e = sc.setdefault(_key(s), {"w": 0, "d": 0, "l": 0})
                if res == "1-0":
                    e["w" if s == "white" else "l"] += 1
                elif res == "0-1":
                    e["w" if s == "black" else "l"] += 1
                else:
                    e["d"] += 1
            db.set_setting("chess_score", _json.dumps(sc))
        return {**_chess_state(), "moved": {"san": san, "uci": uci,
                                           "side": side,
                                           "model": CHESS[side],
                                           "book": from_book,
                                           "vetoes": vetoes,
                                           "reason": reason,
                                           "jab": jab,
                                           "fallback": fb}}
    finally:
        CHESS["busy"] = False


class ChessHuman(BaseModel):
    uci: str
    taunt: str = ""

    @field_validator("uci")
    @classmethod
    def check_uci(cls, v):
        if not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", (v or "").strip().lower()):
            raise ValueError("bad uci")
        return v.strip().lower()


@app.post("/api/chess/human")
def chess_human(body: ChessHuman, x_passcode: str | None = Header(default=None)):
    """The human's own move. Optional taunt (≤140 chars) joins the banter."""
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    b = CHESS["board"]
    side = "white" if b.turn == _chess.WHITE else "black"
    if CHESS["human"] != side:
        raise HTTPException(400, "not your turn")
    if b.is_game_over():
        raise HTTPException(400, "game over")
    try:
        move = b.parse_uci(body.uci)
    except ValueError:
        raise HTTPException(400, "illegal move")
    if move not in b.legal_moves:
        raise HTTPException(400, "illegal move")
    san = b.san(move)
    b.push(move)
    if body.taunt.strip():
        _banter("human", "You", body.taunt.strip()[:140])
    try:
        cp, mate = _quick_eval(b)
        CHESS["last_eval_cp"] = cp
        CHESS["last_eval_mate"] = mate
    except Exception:
        pass
    CHESS["last_reason"] = f"You played {san}."
    return {**_chess_state(), "moved": {"san": san, "uci": body.uci,
                                       "side": side, "model": "You",
                                       "book": False, "vetoes": 0,
                                       "reason": "", "jab": "",
                                       "fallback": False}}


@app.get("/api/chess/legal")
def chess_legal(square: str):
    """Legal target squares from `square` (e.g. e2) for tap-to-move dots."""
    try:
        sq = _chess.parse_square(square.strip().lower())
    except ValueError:
        raise HTTPException(400, "bad square")
    b = CHESS["board"]
    side = "white" if b.turn == _chess.WHITE else "black"
    if CHESS["human"] != side or b.is_game_over():
        return {"targets": []}
    piece = b.piece_at(sq)
    if not piece or piece.color != b.turn:
        return {"targets": []}
    return {"targets": [m.to_square and _chess.square_name(m.to_square)
                        for m in b.legal_moves if m.from_square == sq]}


@app.get("/api/chess/hint")
def chess_hint(x_passcode: str | None = Header(default=None)):
    """Coach's suggested move for the human. Doesn't play it."""
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    b = CHESS["board"]
    side = "white" if b.turn == _chess.WHITE else "black"
    if CHESS["human"] != side or b.is_game_over():
        raise HTTPException(400, "hint makes no sense right now")
    try:
        import chess.engine as _eng
        res = _engine().play(b, _eng.Limit(depth=12))
        mv = res.move
        return {"san": b.san(mv), "uci": mv.uci(),
                "from": _chess.square_name(mv.from_square),
                "to": _chess.square_name(mv.to_square)}
    except Exception:
        raise HTTPException(502, "coach unavailable")


@app.get("/api/chess/pgn")
def chess_pgn(x_passcode: str | None = Header(default=None)):
    """Export current game as PGN string."""
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        import chess.pgn as _pgn
        game = _pgn.Game.from_board(CHESS["board"])
        game.headers["Event"] = "Lamplight AI Arena"
        game.headers["Site"] = "Localhost"
        game.headers["White"] = CHESS.get("white", "White")
        game.headers["Black"] = CHESS.get("black", "Black")
        game.headers["Result"] = CHESS["board"].result() if CHESS["board"].is_game_over() else "*"
        exporter = _pgn.StringExporter(headers=True, variations=True, comments=True)
        return {"pgn": game.accept(exporter)}
    except Exception as e:
        raise HTTPException(500, f"PGN export failed: {e}")


@app.get("/api/chess/analyze")
def chess_analyze(x_passcode: str | None = Header(default=None)):
    """Deep Stockfish analysis of current board position."""
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        import chess.engine as _eng
        lim = _eng.Limit(time=0.35, depth=16)
        info = _engine().analyse(CHESS["board"], lim)
        score = info["score"].pov(_chess.WHITE)
        cp = score.score()
        mate = score.mate() if score.is_mate() else None
        pv = [CHESS["board"].san(m) for m in info.get("pv", [])[:5]]
        best = info.get("pv", [None])[0]
        best_move = {"uci": best.uci(), "san": CHESS["board"].san(best)} if best else None
        win_pct = 50.0
        if mate is not None:
            win_pct = 100.0 if mate > 0 else 0.0
        elif cp is not None:
            win_pct = round(1 / (1 + 10 ** (-cp / 400)) * 100, 1)
        return {
            "cp": cp,
            "mate": mate,
            "win_pct_white": win_pct,
            "best_move": best_move,
            "pv": pv,
            "depth": info.get("depth", 16)
        }
    except Exception as e:
        raise HTTPException(500, f"Stockfish analysis failed: {e}")


@app.get("/api/chess/fen_at")
def chess_fen_at(step: int, x_passcode: str | None = Header(default=None)):
    """Replay moves up to step and return board FEN."""
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    stack = list(CHESS["board"].move_stack)
    total = len(stack)
    target = max(0, min(total, step))
    b = _chess.Board()
    for m in stack[:target]:
        b.push(m)
    return {
        "fen": b.fen(),
        "step": target,
        "total": total,
        "turn": "white" if b.turn == _chess.WHITE else "black",
        "last_move": stack[target - 1].uci() if target > 0 else None
    }


@app.get("/api/history")
def get_history(mode: str, limit: int = 100, session_id: str | None = None,
                x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if mode not in db.MODES:
        raise HTTPException(400, "bad mode")
    if not session_id:
        session_id = db.get_or_create_latest_session(mode)
    sess = db.get_session(session_id)
    return {
        "messages": db.history(mode, min(max(limit, 1), 200), session_id=session_id),
        "session_id": session_id,
        "session_title": sess.get("title") if sess else "Chat"
    }


@app.get("/api/sessions")
def sessions_list(mode: str = "nsfw", workspace_id: str | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if mode not in db.MODES:
        raise HTTPException(400, "bad mode")
    return {"sessions": db.list_sessions(mode, workspace_id=workspace_id)}


@app.post("/api/sessions")
def session_create(body: SessionCreateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if body.mode not in db.MODES:
        raise HTTPException(400, "bad mode")
    sid = db.create_session(body.mode, body.title or "New Chat", workspace_id=body.workspace_id or "ws_default")
    s = db.get_session(sid)
    return {"ok": True, "session": s}


@app.patch("/api/sessions/{session_id}")
def session_rename(session_id: str, body: SessionRenameIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    s = db.get_session(session_id)
    if not s:
        raise HTTPException(404, "session not found")
    db.rename_session(session_id, body.title)
    return {"ok": True, "session": db.get_session(session_id)}


@app.delete("/api/sessions/{session_id}")
def session_delete(session_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    s = db.get_session(session_id)
    if not s:
        raise HTTPException(404, "session not found")
    db.delete_session(session_id)
    return {"ok": True}


@app.get("/api/backup")
def backup(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return FileResponse(db.DB_PATH, filename="lamplight-backup.db",
                        media_type="application/x-sqlite3")


@app.get("/api/export")
def export_chat(mode: str, session_id: str | None = None,
                x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if mode not in ("nsfw", "friend"):
        raise HTTPException(400, "bad mode")
    import io
    msgs = db.history(mode, 10000, session_id=session_id)
    sess = db.get_session(session_id) if session_id else None
    sess_title = sess.get("title") if sess else None
    title = sess_title or ("After Dark" if mode == "nsfw" else "Friend")
    buf = io.StringIO()
    buf.write(f"# {title} — exported chat\n\n")
    for m in msgs:
        who = "**You**" if m["role"] == "user" else f"**{title}**"
        buf.write(f"{who} ({m['created_at'] or ''}):\n{m['content']}\n\n---\n\n")
    data = buf.getvalue().encode()
    from fastapi.responses import Response
    clean_name = "".join(c for c in (sess_title or mode) if c.isalnum() or c in ("-", "_")).rstrip()
    fname = f"{clean_name or mode}-chat.md"
    return Response(content=data, media_type="text/markdown",
                    headers={"Content-Disposition":
                             f"attachment; filename={fname}"})


class ClearIn(BaseModel):
    mode: str


@app.post("/api/clear")
def clear(body: ClearIn):
    if body.mode not in db.MODES:
        raise HTTPException(400, "bad mode")
    db.clear_mode(body.mode)
    return {"ok": True}


def fetch_provider_models(provider: dict) -> list[dict]:
    p_type = provider.get("provider_type", "ollama")
    base_url = provider.get("base_url", "").rstrip("/")
    api_key = provider.get("api_key", "").strip()
    pid = provider.get("id", "p_ollama")
    pname = provider.get("name", "Local")

    if p_type == "ollama":
        try:
            r = requests.get(f"{base_url}/api/tags", timeout=6)
            if r.status_code == 200:
                models = []
                for m in r.json().get("models", []):
                    caps = get_model_thinking_capabilities(m["name"], provider_type="ollama", base_url=base_url)
                    models.append({
                        "name": m["name"],
                        "size": m.get("size", 0),
                        "provider_id": pid,
                        "provider_name": pname,
                        "provider_type": "ollama",
                        "full_id": f"{m['name']}::{pid}" if pid != "p_ollama" else m["name"],
                        "supports_thinking": caps["supports_thinking"],
                        "supported_levels": caps["supported_levels"],
                        "thinking_summary": caps["summary"]
                    })
                return models
        except Exception:
            return []

    elif p_type == "openai_compat":
        try:
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            r = requests.get(f"{base_url}/v1/models", headers=headers, timeout=6)
            if r.status_code == 200:
                models = []
                for m in r.json().get("data", []):
                    mid = m.get("id")
                    if mid:
                        caps = get_model_thinking_capabilities(mid, provider_type="openai_compat", base_url=base_url)
                        models.append({
                            "name": mid,
                            "size": m.get("size", 0),
                            "provider_id": pid,
                            "provider_name": pname,
                            "provider_type": "openai_compat",
                            "full_id": f"{mid}::{pid}",
                            "supports_thinking": caps["supports_thinking"],
                            "supported_levels": caps["supported_levels"],
                            "thinking_summary": caps["summary"]
                        })
                return models
        except Exception:
            return []

    return []


@app.get("/api/models")
def models():
    providers = db.list_providers(active_only=True)
    all_models = []
    seen = set()
    for p in providers:
        p_models = fetch_provider_models(p)
        for m in p_models:
            key = (m["name"], m["provider_id"])
            if key not in seen:
                seen.add(key)
                all_models.append(m)

    per_mode = {m: (db.get_setting(f"model_{m}") or DEFAULT_MODELS[m])
                for m in db.MODES}
    return {
        "models": all_models,
        "providers": providers,
        "per_mode": per_mode
    }


@app.get("/api/model-capabilities")
def model_capabilities(model: str = Query(..., description="Model name or full identifier")):
    return get_model_thinking_capabilities(model)


# ============================================================================
# Model Intelligence, Router, Presets & Prompt Debugger API
# ============================================================================

@app.get("/api/models/profiles")
def get_model_profiles(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    avail = [m["name"] for m in _get_available_model_names()]
    profiles = model_intelligence.get_all_model_profiles(avail)
    return {"ok": True, "profiles": profiles}


@app.post("/api/router/analyze")
def router_analyze(body: RouterAnalyzeIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    task_type, conf = model_intelligence.detect_task_type(body.prompt, body.attachments, body.explicit_task)
    avail = [m["name"] for m in _get_available_model_names()]
    best_model, route_meta = model_intelligence.route_model(task_type, "auto", avail)
    return {
        "ok": True,
        "task_type": task_type,
        "confidence": conf,
        "recommended_model": best_model,
        "routing_meta": route_meta
    }


@app.get("/api/router/headroom")
def router_headroom(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"ok": True, "headroom": model_intelligence.get_system_resource_headroom()}


@app.post("/api/router/eject")
def router_eject(keep_model: str | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    evicted = model_intelligence.eject_idle_models(keep_model=keep_model)
    return {"ok": True, "evicted": evicted}


@app.get("/api/presets")
def get_presets(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"ok": True, "presets": model_intelligence.list_generation_presets()}


@app.post("/api/presets")
def save_preset(body: PresetIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if not body.name.strip():
        raise HTTPException(400, "Preset name required")
    res = model_intelligence.save_generation_preset(
        name=body.name,
        description=body.description,
        category=body.category,
        temperature=body.temperature,
        top_p=body.top_p,
        presence_penalty=body.presence_penalty,
        frequency_penalty=body.frequency_penalty,
        num_predict=body.num_predict,
        thinking_level=body.thinking_level,
        system_prefix=body.system_prefix,
        preset_id=body.id
    )
    return {"ok": True, "preset": res}


@app.delete("/api/presets/{preset_id}")
def delete_preset(preset_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    ok = model_intelligence.delete_generation_preset(preset_id)
    return {"ok": ok}


@app.get("/api/schemas/standard")
def get_standard_schemas(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"ok": True, "schemas": model_intelligence.STANDARD_SCHEMAS}


@app.get("/api/debugger/records")
def get_debugger_records(limit: int = 50, session_id: str | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    records = model_intelligence.list_request_records(limit=limit, session_id=session_id)
    return {"ok": True, "records": records}


@app.get("/api/debugger/records/{req_id}")
def get_debugger_record_detail(req_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    rec = model_intelligence.get_request_record(req_id)
    if not rec:
        raise HTTPException(404, "record not found")
    return {"ok": True, "record": rec}


@app.get("/api/debugger/diff")
def get_debugger_diff(id_a: str, id_b: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    diff_res = model_intelligence.diff_request_records(id_a, id_b)
    return {"ok": True, "diff": diff_res}


@app.post("/api/chat/verify")
def chat_verify(body: VerificationIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    res = model_intelligence.generate_answer_verification(body.user_prompt, body.assistant_reply, body.critic_model)
    return {"ok": True, "verification": res}


# ---------- Providers API ----------
class ProviderIn(BaseModel):
    name: str
    provider_type: str = "openai_compat"
    base_url: str
    api_key: str = ""


class ProviderUpdateIn(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    is_active: bool | None = None


@app.get("/api/providers")
def get_providers(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"providers": db.list_providers()}


@app.post("/api/providers")
def add_provider(body: ProviderIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        p = db.create_provider(body.name, body.provider_type, body.base_url, body.api_key)
        return {"ok": True, "provider": p}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.put("/api/providers/{pid}")
def edit_provider(pid: str, body: ProviderUpdateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        p = db.update_provider(pid, body.name, body.base_url, body.api_key, 1 if body.is_active else 0 if body.is_active is not None else None)
        if not p:
            raise HTTPException(404, "Provider not found")
        return {"ok": True, "provider": p}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/api/providers/{pid}")
def remove_provider(pid: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        ok = db.delete_provider(pid)
        return {"ok": ok}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- Model Lab & Benchmarking API ----------
class LabRunIn(BaseModel):
    prompt: str
    models: list[str]
    mode: str = "transparent"  # "transparent", "blind"
    execution_mode: str = "sequential"  # "sequential", "parallel"
    suite_name: str | None = None
    temperature: float = 0.7
    system_prompt: str | None = None


class LabVoteIn(BaseModel):
    experiment_id: str
    winner_model: str
    ratings: dict[str, int] | None = None


class LabSuiteIn(BaseModel):
    name: str
    category: str = "Custom"
    prompt: str


def _run_model_completion(model_identifier: str, prompt: str, system_prompt: str | None = None,
                          temperature: float = 0.7, timeout: int = 180) -> dict:
    if "::" in model_identifier:
        model_name, provider_id = model_identifier.split("::", 1)
    else:
        model_name = model_identifier
        provider_id = "p_ollama"

    provider = db.get_provider(provider_id)
    if not provider:
        provider = {"id": "p_ollama", "provider_type": "ollama", "base_url": OLLAMA_URL, "api_key": "", "name": "Local"}

    p_type = provider.get("provider_type", "ollama")
    base_url = provider.get("base_url", OLLAMA_URL).rstrip("/")
    api_key = provider.get("api_key", "").strip()

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    vram_before = 0
    try:
        vram_before = sum(m.get("size_vram", 0) for m in _get_loaded_models()) / (1024 * 1024)
    except Exception:
        pass

    t0 = time.perf_counter()
    ttft_ms = 0
    response_text = ""
    tokens_prompt = max(int(len(prompt.split()) * 1.3), 1)
    tokens_completion = 0

    if p_type == "ollama":
        payload = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature}
        }
        with requests.post(f"{base_url}/api/chat", json=payload, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Ollama error ({r.status_code}): {r.text[:200]}")
            for line in r.iter_lines():
                if line:
                    if ttft_ms == 0:
                        ttft_ms = int((time.perf_counter() - t0) * 1000)
                    chunk = json.loads(line)
                    msg = chunk.get("message", {})
                    response_text += msg.get("content", "")
                    if chunk.get("done"):
                        if chunk.get("prompt_eval_count"):
                            tokens_prompt = chunk["prompt_eval_count"]
                        if chunk.get("eval_count"):
                            tokens_completion = chunk["eval_count"]
                        break
    elif p_type == "openai_compat":
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "stream": True
        }
        with requests.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Provider error ({r.status_code}): {r.text[:200]}")
            for line in r.iter_lines():
                if line:
                    line_str = line.decode("utf-8").strip()
                    if line_str.startswith("data: "):
                        data_part = line_str[6:]
                        if data_part == "[DONE]":
                            break
                        try:
                            c = json.loads(data_part)
                            if ttft_ms == 0:
                                ttft_ms = int((time.perf_counter() - t0) * 1000)
                            delta = c.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            response_text += content
                        except Exception:
                            pass

    total_duration_s = max(time.perf_counter() - t0, 0.001)
    duration_ms = int(total_duration_s * 1000)
    if ttft_ms == 0:
        ttft_ms = duration_ms

    if tokens_completion == 0:
        tokens_completion = max(int(len(response_text) / 3.8), 1)

    tps = tokens_completion / total_duration_s

    vram_after = 0
    try:
        vram_after = sum(m.get("size_vram", 0) for m in _get_loaded_models()) / (1024 * 1024)
    except Exception:
        pass
    vram_delta = max(0.0, vram_after - vram_before)

    db.log_inference(
        model=model_name,
        provider=provider.get("name", "Local"),
        prompt_chars=len(prompt),
        response_chars=len(response_text),
        duration_ms=duration_ms,
        ttft_ms=ttft_ms,
        tokens_per_sec=round(tps, 2),
        status="ok"
    )

    return {
        "model_name": model_name,
        "full_id": model_identifier,
        "provider_id": provider_id,
        "provider_name": provider.get("name", "Local"),
        "response_text": response_text,
        "tokens_prompt": tokens_prompt,
        "tokens_completion": tokens_completion,
        "duration_ms": duration_ms,
        "ttft_ms": ttft_ms,
        "tokens_per_sec": round(tps, 2),
        "vram_delta_mb": round(vram_delta, 1)
    }


@app.post("/api/lab/run")
def lab_run(body: LabRunIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if not body.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    if not (1 <= len(body.models) <= 4):
        raise HTTPException(400, "Select between 1 and 4 models to compare")

    exp_id = f"exp_{uuid.uuid4().hex[:12]}"
    db.create_experiment(
        exp_id=exp_id,
        prompt=body.prompt.strip(),
        suite_name=body.suite_name or "Custom",
        mode=body.mode,
        execution_mode=body.execution_mode
    )

    blind_labels = ["Model Alpha", "Model Beta", "Model Gamma", "Model Delta"]
    runs = []

    if body.execution_mode == "parallel" and len(body.models) > 1:
        with ThreadPoolExecutor(max_workers=len(body.models)) as executor:
            future_to_model = {
                executor.submit(_run_model_completion, m, body.prompt, body.system_prompt, body.temperature): m
                for m in body.models
            }
            for future in as_completed(future_to_model):
                m_orig = future_to_model[future]
                try:
                    res = future.result()
                    runs.append(res)
                except Exception as e:
                    runs.append({
                        "model_name": m_orig,
                        "full_id": m_orig,
                        "provider_id": "error",
                        "provider_name": "Error",
                        "response_text": f"[Error: {e}]",
                        "tokens_prompt": 0,
                        "tokens_completion": 0,
                        "duration_ms": 0,
                        "ttft_ms": 0,
                        "tokens_per_sec": 0.0,
                        "vram_delta_mb": 0.0
                    })
    else:
        for m in body.models:
            if body.execution_mode == "sequential":
                try:
                    _eject_all_loaded()
                except Exception:
                    pass
            try:
                res = _run_model_completion(m, body.prompt, body.system_prompt, body.temperature)
                runs.append(res)
            except Exception as e:
                runs.append({
                    "model_name": m,
                    "full_id": m,
                    "provider_id": "error",
                    "provider_name": "Error",
                    "response_text": f"[Error: {e}]",
                    "tokens_prompt": 0,
                    "tokens_completion": 0,
                    "duration_ms": 0,
                    "ttft_ms": 0,
                    "tokens_per_sec": 0.0,
                    "vram_delta_mb": 0.0
                })

    saved_runs = []
    for idx, r in enumerate(runs):
        rid = db.add_experiment_run(
            exp_id=exp_id,
            model_name=r["model_name"],
            provider_id=r["provider_id"],
            response_text=r["response_text"],
            tokens_prompt=r["tokens_prompt"],
            tokens_completion=r["tokens_completion"],
            duration_ms=r["duration_ms"],
            ttft_ms=r["ttft_ms"],
            tokens_per_sec=r["tokens_per_sec"],
            vram_delta_mb=r["vram_delta_mb"]
        )
        r_copy = dict(r)
        r_copy["run_id"] = rid
        if body.mode == "blind":
            r_copy["blind_label"] = blind_labels[idx] if idx < len(blind_labels) else f"Model {idx+1}"
        saved_runs.append(r_copy)

    return {
        "ok": True,
        "experiment_id": exp_id,
        "mode": body.mode,
        "runs": saved_runs
    }


@app.post("/api/lab/vote")
def lab_vote(body: LabVoteIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    res = db.vote_experiment(body.experiment_id, body.winner_model, body.ratings)
    if not res:
        raise HTTPException(404, "Experiment not found")
    return {"ok": True, "experiment": res}


@app.get("/api/lab/experiments")
def lab_experiments(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"experiments": db.list_experiments(limit=50)}


@app.get("/api/lab/experiments/{exp_id}")
def lab_get_experiment(exp_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    exp = db.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, "Experiment not found")
    return {"experiment": exp}


@app.get("/api/lab/suites")
def lab_suites(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"suites": db.list_lab_suites()}


@app.post("/api/lab/suites")
def lab_suite_create(body: LabSuiteIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        suite = db.create_lab_suite(body.name, body.category, body.prompt)
        return {"ok": True, "suite": suite}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/api/lab/suites/{sid}")
def lab_suite_delete(sid: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        ok = db.delete_lab_suite(sid)
        return {"ok": ok}
    except ValueError as e:
        raise HTTPException(400, str(e))


class ImportIn(BaseModel):
    name: str
    gguf_path: str

    @field_validator("name")
    @classmethod
    def check_name(cls, v):
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError("name: letters/numbers/._:- , max 64")
        return v


@app.post("/api/import")
def import_gguf(body: ImportIn):
    """Import a .gguf from a LAPTOP-local path into Ollama. No browser uploads."""
    p = Path(body.gguf_path.strip()).expanduser()
    if p.suffix.lower() != ".gguf":
        raise HTTPException(400, "path must end in .gguf")
    if not p.is_file():
        raise HTTPException(400, f"not found on server: {p}")
    if p.stat().st_size < 10_000_000:
        raise HTTPException(400, "file too small to be a model")
    with tempfile.NamedTemporaryFile("w", suffix="Modelfile",
                                     delete=False) as f:
        f.write(f"FROM {p}\n")
        f.write('TEMPLATE """{{- if .System }}<|im_start|>system\n{{ .System }}<|im_end|>\n{{ end }}{{- range .Messages }}<|im_start|>{{ .Role }}\n{{ .Content }}<|im_end|>\n{{ end }}<|im_start|>assistant\n"""\n')
        f.write('PARAMETER stop "<|im_start|>"\n')
        f.write('PARAMETER stop "<|im_end|>"\n')
        modelfile = f.name
    try:
        proc = subprocess.run(["ollama", "create", body.name, "-f", modelfile],
                              capture_output=True, text=True, timeout=600)
    finally:
        Path(modelfile).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise HTTPException(500, f"ollama create failed: {proc.stderr[:500]}")
    return {"ok": True, "name": body.name}


class ModeModelIn(BaseModel):
    mode: str
    model: str


@app.post("/api/mode-model")
def set_mode_model(body: ModeModelIn):
    if body.mode not in db.MODES or not NAME_RE.match(body.model.strip()):
        raise HTTPException(400, "bad mode/model")
    db.set_setting(f"model_{body.mode}", body.model.strip())
    return {"ok": True}


@app.post("/api/confirm-18")
def confirm_18():
    db.set_setting("18plus_ok", "1")
    return {"ok": True}


@app.get("/api/gate")
def gate():
    return {"need18": False,
            "hasPasscode": db.get_setting("passcode_hash") is not None}


class PassIn(BaseModel):
    code: str


@app.post("/api/passcode/set")
def passcode_set(body: PassIn):
    if not 4 <= len(body.code) <= 64:
        raise HTTPException(400, "passcode 4-64 chars")
    db.set_setting("passcode_hash",
                   hashlib.sha256(body.code.encode()).hexdigest())
    return {"ok": True}


@app.post("/api/passcode/unlock")
def passcode_unlock(body: PassIn):
    h = db.get_setting("passcode_hash")
    if h and hashlib.sha256(body.code.encode()).hexdigest() == h:
        return {"ok": True, "token": body.code}
    raise HTTPException(401, "wrong passcode")


class FriendProfileIn(BaseModel):
    name: str = ""
    about: str = ""

    @field_validator("name")
    @classmethod
    def check_name(cls, v):
        if len(v) > 60:
            raise ValueError("name max 60 chars")
        return v.strip()

    @field_validator("about")
    @classmethod
    def check_about(cls, v):
        if len(v) > 2000:
            raise ValueError("about max 2000 chars")
        return v.strip()


@app.get("/api/friend/profile")
def friend_profile_get():
    return {"profile": db.get_profile(),
            "summary": db.get_setting("friend_summary")}


@app.post("/api/friend/profile")
def friend_profile_set(body: FriendProfileIn):
    db.set_profile_field("name", body.name)
    db.set_profile_field("about", body.about)
    return {"ok": True}


@app.post("/api/friend/forget")
def friend_forget():
    db.clear_profile()
    db.set_setting("friend_summary", "")
    db.clear_facts()
    db.clear_mode("friend")
    return {"ok": True}


class FactIn(BaseModel):
    fact: str


@app.get("/api/friend/facts")
def friend_facts_get():
    return {"facts": db.list_facts()}


@app.post("/api/friend/facts")
def friend_fact_add(body: FactIn):
    if not 1 <= len(body.fact.strip()) <= 200:
        raise HTTPException(400, "fact must be 1-200 chars")
    fid = db.add_fact(body.fact)
    return {"ok": True, "id": fid}


@app.post("/api/friend/facts/delete")
def friend_fact_del(body: dict):
    try:
        fid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "bad id")
    db.del_fact(fid)
    return {"ok": True}


class PromptIn(BaseModel):
    mode: str
    prompt: str


@app.get("/api/prompts")
def prompts_get():
    out = {}
    for m in ("nsfw", "friend", "architect", "muse"):
        eff = _nsfw_system() if m == "nsfw" else (_friend_system() if m == "friend" else (_architect_system() if m == "architect" else _muse_system()))
        out[m] = {"custom": db.get_setting(f"prompt_{m}") or "",
                  "effective": eff}
    return {"prompts": out}


@app.post("/api/prompts")
def prompt_set(body: PromptIn):
    if body.mode not in ("nsfw", "friend", "architect", "muse"):
        raise HTTPException(400, "bad mode")
    if len(body.prompt) > 2000:
        raise HTTPException(400, "prompt max 2000 chars")
    db.set_setting(f"prompt_{body.mode}", body.prompt.strip())
    return {"ok": True}


@app.post("/api/prompts/reset")
def prompt_reset(body: dict):
    mode = body.get("mode")
    if mode not in ("nsfw", "friend", "architect", "muse"):
        raise HTTPException(400, "bad mode")
    db.set_setting(f"prompt_{mode}", "")
    eff = _nsfw_system() if mode == "nsfw" else (_friend_system() if mode == "friend" else (_architect_system() if mode == "architect" else _muse_system()))
    return {"ok": True, "effective": eff}


class ArchitectStackIn(BaseModel):
    stack: str
    rules: str | None = ""


@app.get("/api/architect/stack")
def architect_stack_get():
    return {
        "stack": db.get_setting("architect_stack") or "",
        "rules": db.get_setting("architect_rules") or ""
    }


@app.post("/api/architect/stack")
def architect_stack_set(body: ArchitectStackIn):
    db.set_setting("architect_stack", body.stack.strip()[:3000])
    if body.rules is not None:
        db.set_setting("architect_rules", body.rules.strip()[:3000])
    return {"ok": True}


@app.post("/api/architect/clear")
def architect_clear():
    db.set_setting("architect_stack", "")
    db.set_setting("architect_rules", "")
    db.clear_mode("architect")
    return {"ok": True}


class MuseBibleIn(BaseModel):
    bible: str | None = ""
    characters: str | None = ""
    rules: str | None = ""


@app.get("/api/muse/bible")
def muse_bible_get():
    return {
        "bible": db.get_setting("muse_bible") or "",
        "characters": db.get_setting("muse_characters") or "",
        "rules": db.get_setting("muse_rules") or ""
    }


@app.post("/api/muse/bible")
def muse_bible_set(body: MuseBibleIn):
    if body.bible is not None:
        db.set_setting("muse_bible", body.bible.strip()[:5000])
    if body.characters is not None:
        db.set_setting("muse_characters", body.characters.strip()[:4000])
    if body.rules is not None:
        db.set_setting("muse_rules", body.rules.strip()[:3000])
    return {"ok": True}


@app.post("/api/muse/clear")
def muse_clear():
    db.set_setting("muse_bible", "")
    db.set_setting("muse_characters", "")
    db.set_setting("muse_rules", "")
    db.clear_mode("muse")
    return {"ok": True}


# ==========================================================================
# AI Arena: Council Jury & Adversarial Debates
# ==========================================================================
class CouncilRunIn(BaseModel):
    topic: str
    juror_models: list[str] = []
    jurors: list[str] = []
    judge_model: str = ""
    judge: str = ""
    execution_mode: str = "sequential"
    mode: str = "sequential"


class DebateRunIn(BaseModel):
    topic: str
    model_a: str
    model_b: str
    judge_model: str = ""
    judge: str = ""
    rounds: int = 2


@app.post("/api/arena/council/run")
def arena_council_run(body: CouncilRunIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    topic = body.topic.strip()
    juror_models = body.juror_models or body.jurors
    judge_model = body.judge_model or body.judge
    execution_mode = body.mode if body.mode != "sequential" else body.execution_mode
    if not topic:
        raise HTTPException(400, "Topic cannot be empty")
    if len(juror_models) < 2:
        raise HTTPException(400, "Please select at least 2 juror models")
    if not judge_model:
        raise HTTPException(400, "Please select a Chief Synthesizer model")

    juror_system = (
        "You are an expert specialist serving on an elite AI Advisory Council.\n"
        "Analyze the provided question or dilemma with depth, rigor, and nuanced trade-offs.\n"
        "State your position clearly, present concrete rationale, and provide actionable takeaways."
    )
    jurors_out = []
    if execution_mode == "parallel":
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(juror_models))) as ex:
            futures = [ex.submit(_run_model_completion, m, topic, juror_system, 0.7) for m in juror_models]
            for f in futures:
                try:
                    jurors_out.append(f.result())
                except Exception as e:
                    jurors_out.append({"model_name": "Error", "response_text": f"Error: {e}", "tokens_per_sec": 0, "duration_ms": 0})
    else:
        for m in juror_models:
            _eject_all_loaded()
            try:
                res = _run_model_completion(m, topic, juror_system, 0.7)
                jurors_out.append(res)
            except Exception as e:
                jurors_out.append({"model_name": m, "response_text": f"Error: {e}", "tokens_per_sec": 0, "duration_ms": 0})
            _eject_all_loaded()

    statements_text = "\n\n".join([
        f"--- STATEMENT BY JUROR [{j.get('model_name', 'Juror')}] ---\n{j.get('response_text', '')}"
        for j in jurors_out
    ])
    synthesizer_system = (
        "You are the Chief Synthesizer of the AI Advisory Council.\n"
        "Multiple AI experts have evaluated the question below. Analyze their arguments and formulate an authoritative synthesis report.\n"
        "You MUST structure your report using the following markdown sections:\n\n"
        "### 🤝 Points of Consensus\n"
        "(Highlight key principles and recommendations where the jurors agree)\n\n"
        "### ⚡ Divergence & Core Disagreements\n"
        "(Analyze conflicting perspectives, divergent risk assessments, and opposing trade-offs)\n\n"
        "### 🏆 Chief Synthesizer Verdict & Recommendation\n"
        "(Provide your final synthesized judgment and prioritized actionable recommendations)"
    )
    synth_prompt = (
        f"COUNCIL QUESTION:\n{topic}\n\n"
        f"JUROR PERSPECTIVES:\n{statements_text}\n\n"
        "Synthesize the council's findings now."
    )
    if execution_mode == "sequential":
        _eject_all_loaded()
    synth_result = _run_model_completion(judge_model, synth_prompt, synthesizer_system, 0.6)
    if execution_mode == "sequential":
        _eject_all_loaded()

    session = db.create_arena_session(
        arena_type="council",
        title=topic[:60],
        topic=topic,
        judge_model=judge_model,
        models=juror_models,
        rounds=jurors_out,
        verdict_text=synth_result.get("response_text", ""),
        verdict_json=synth_result
    )
    return {"ok": True, "session": session}


@app.post("/api/arena/debate/run")
def arena_debate_run(body: DebateRunIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(400, "Topic cannot be empty")
    judge_model = body.judge_model or body.judge
    if not body.model_a or not body.model_b:
        raise HTTPException(400, "Both Affirmative and Negative models must be specified")
    if not judge_model:
        raise HTTPException(400, "Judge model must be specified")

    rounds_count = max(1, min(3, body.rounds))
    debate_rounds = []

    # Round 1: Opening statements
    sys_a_open = f"You are the AFFIRMATIVE debater in a formal debate.\nMotion: '{topic}'\nDeliver a powerful, structured opening constructive argument (2-3 paragraphs) defending the affirmative proposition."
    sys_b_open = f"You are the NEGATIVE debater in a formal debate.\nMotion: '{topic}'\nDeliver a powerful, structured opening constructive argument (2-3 paragraphs) opposing the affirmative proposition."

    _eject_all_loaded()
    res_a_r1 = _run_model_completion(body.model_a, f"Defend the motion: '{topic}'", sys_a_open, 0.7)
    _eject_all_loaded()
    res_b_r1 = _run_model_completion(body.model_b, f"Oppose the motion: '{topic}'", sys_b_open, 0.7)
    _eject_all_loaded()

    debate_rounds.append({
        "round_num": 1,
        "round_name": "Opening Statements",
        "affirmative": res_a_r1,
        "negative": res_b_r1
    })

    # Round 2: Rebuttal & Cross-Examination
    if rounds_count >= 2:
        sys_a_reb = f"You are the AFFIRMATIVE debater.\nMotion: '{topic}'\nDirectly rebut and dismantle the Negative's opening argument below, while reinforcing your core premise."
        prompt_a_reb = f"NEGATIVE'S ARGUMENT TO REBUT:\n{res_b_r1.get('response_text', '')}\n\nDeliver your affirmative rebuttal now."
        res_a_r2 = _run_model_completion(body.model_a, prompt_a_reb, sys_a_reb, 0.7)
        _eject_all_loaded()

        sys_b_reb = f"You are the NEGATIVE debater.\nMotion: '{topic}'\nDirectly rebut the Affirmative's rebuttal below and defend your position against their counter-attacks."
        prompt_b_reb = f"AFFIRMATIVE'S REBUTTAL TO COUNTER:\n{res_a_r2.get('response_text', '')}\n\nDeliver your negative rebuttal now."
        res_b_r2 = _run_model_completion(body.model_b, prompt_b_reb, sys_b_reb, 0.7)
        _eject_all_loaded()

        debate_rounds.append({
            "round_num": 2,
            "round_name": "Rebuttal & Cross-Examination",
            "affirmative": res_a_r2,
            "negative": res_b_r2
        })

    # Round 3: Closing Arguments
    if rounds_count >= 3:
        sys_a_close = f"You are the AFFIRMATIVE debater.\nMotion: '{topic}'\nDeliver your closing speech. Summarize why the affirmative side carried the debate."
        res_a_r3 = _run_model_completion(body.model_a, "Deliver your final closing speech.", sys_a_close, 0.7)
        _eject_all_loaded()

        sys_b_close = f"You are the NEGATIVE debater.\nMotion: '{topic}'\nDeliver your closing speech. Summarize why the negative side carried the debate."
        res_b_r3 = _run_model_completion(body.model_b, "Deliver your final closing speech.", sys_b_close, 0.7)
        _eject_all_loaded()

        debate_rounds.append({
            "round_num": 3,
            "round_name": "Closing Arguments",
            "affirmative": res_a_r3,
            "negative": res_b_r3
        })

    # Judge Decision & Scorecard
    transcript_parts = []
    for rnd in debate_rounds:
        transcript_parts.append(f"=== ROUND {rnd['round_num']}: {rnd['round_name']} ===")
        transcript_parts.append(f"[AFFIRMATIVE ({body.model_a})]:\n{rnd['affirmative'].get('response_text', '')}\n")
        transcript_parts.append(f"[NEGATIVE ({body.model_b})]:\n{rnd['negative'].get('response_text', '')}\n")

    full_transcript = "\n".join(transcript_parts)
    judge_system = (
        "You are an impartial Grand Arbiter and Debate Judge presiding over a formal debate.\n"
        "Evaluate both debaters with strict objectivity based on:\n"
        "1. Logic & Soundness of Arguments\n"
        "2. Rhetoric, Persuasiveness & Clarity\n"
        "3. Effectiveness of Rebuttals\n\n"
        "Format your verdict clearly with the following sections:\n"
        "### 📊 Scorecard\n"
        f"- **Affirmative ({body.model_a})**: Logic: X/10 | Rhetoric: X/10 | Rebuttal: X/10 | Total: Y/30\n"
        f"- **Negative ({body.model_b})**: Logic: X/10 | Rhetoric: X/10 | Rebuttal: X/10 | Total: Y/30\n\n"
        "### 🏆 Winner Declaration\n"
        "**Winner: [Affirmative / Negative / Draw]**\n\n"
        "### ⚖️ Judge's Rationale\n"
        "(Explain why the winning side prevailed, citing specific arguments that swung the debate)"
    )
    judge_prompt = f"DEBATE MOTION: '{topic}'\n\nTRANSCRIPT:\n{full_transcript}\n\nDeliver your official judge's scorecard and verdict now."
    judge_result = _run_model_completion(judge_model, judge_prompt, judge_system, 0.5)
    _eject_all_loaded()

    session = db.create_arena_session(
        arena_type="debate",
        title=topic[:60],
        topic=topic,
        judge_model=judge_model,
        models=[body.model_a, body.model_b],
        rounds=debate_rounds,
        verdict_text=judge_result.get("response_text", ""),
        verdict_json=judge_result
    )
    return {"ok": True, "session": session}


@app.get("/api/arena/sessions")
def arena_list_sessions(arena_type: str | None = None, mode: str | None = None, limit: int = 50, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    chosen_type = arena_type or mode
    if chosen_type == "all":
        chosen_type = None
    return {"sessions": db.list_arena_sessions(arena_type=chosen_type, limit=limit)}


@app.get("/api/arena/sessions/{sid}")
def arena_get_session(sid: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    s = db.get_arena_session(sid)
    if not s:
        raise HTTPException(404, "Arena session not found")
    return {"session": s}


@app.delete("/api/arena/sessions/{sid}")
def arena_delete_session(sid: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"ok": db.delete_arena_session(sid)}


# ==========================================================================
# Document Hub & Study Workflows (Phase C)
# ==========================================================================
# Document Hub & Study Workflows (Phase C)
# ==========================================================================
class StudyRequest(BaseModel):
    study_type: str | None = None
    mode: str | None = None
    model: str | None = None
    count: int = 10

    @property
    def effective_type(self) -> str:
        return (self.study_type or self.mode or "flashcards").lower()


class DocumentCompareRequest(BaseModel):
    doc_a_id: str | None = None
    doc_b_id: str | None = None
    doc_id_a: str | None = None
    doc_id_b: str | None = None
    model: str | None = None
    focus: str | None = None

    @property
    def id_a(self) -> str:
        return self.doc_a_id or self.doc_id_a or ""

    @property
    def id_b(self) -> str:
        return self.doc_b_id or self.doc_id_b or ""


class CanvasSnapshotRequest(BaseModel):
    title: str
    lang: str
    code: str
    parent_id: str | None = None
    version: int = 1
    description: str | None = None


class CanvasZipExportRequest(BaseModel):
    title: str | None = "web_project"
    filename: str | None = None
    files: dict[str, str] | list[dict[str, str]]


@app.get("/api/documents")
def list_documents(limit: int = 100, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"documents": db.list_library_documents(limit=limit)}


@app.post("/api/documents/upload")
async def upload_document(
    request: Request,
    x_passcode: str | None = Header(default=None)
):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    
    filename = "uploaded_file"
    content = b""
    content_type = ""

    ct = request.headers.get("content-type", "")
    if "multipart/form-data" in ct:
        form = await request.form()
        uploaded_file = form.get("file")
        if uploaded_file and hasattr(uploaded_file, "filename"):
            filename = uploaded_file.filename or "uploaded_file"
            content = await uploaded_file.read()
            content_type = getattr(uploaded_file, "content_type", "") or ""
    else:
        try:
            body = await request.json()
            filename = body.get("filename", "uploaded_file")
            b64_str = body.get("content_b64", "")
            if b64_str:
                content = base64.b64decode(b64_str)
        except Exception:
            pass

    if not content:
        raise HTTPException(400, "No file or file content provided")

    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)
    doc_id = f"doc_{uuid.uuid4().hex[:10]}"
    target_path = UPLOAD_DIR / f"{doc_id}_{safe_name}"
    target_path.write_bytes(content)

    meta = parse_uploaded_file(target_path, filename, content_type)
    doc = db.save_library_document(
        doc_id=doc_id,
        filename=filename,
        file_type=meta.get("ext", Path(safe_name).suffix.lower()),
        file_size=len(content),
        preview_url=meta.get("preview_url", f"/uploads/{target_path.name}"),
        char_count=meta.get("char_count", 0),
        page_count=meta.get("page_count"),
        extracted_text=meta.get("text_content", "")
    )
    return {"ok": True, "document": doc}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    doc = db.get_library_document(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return {"document": doc}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    doc = db.get_library_document(doc_id)
    if doc and doc.get("preview_url"):
        try:
            fname = Path(doc["preview_url"]).name
            fpath = UPLOAD_DIR / fname
            if fpath.exists():
                fpath.unlink()
        except Exception:
            pass
    return {"ok": db.delete_library_document(doc_id)}


@app.post("/api/documents/{doc_id}/study")
def generate_study_material(doc_id: str, req: StudyRequest, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    doc = db.get_library_document(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")

    text = (doc.get("extracted_text") or "").strip()
    if not text:
        raise HTTPException(400, "Document contains no extractable text content")

    active_model = req.model or DEFAULT_MODELS.get("friend", "llama3.2:3b")
    study_type = req.effective_type

    if study_type == "flashcards":
        sys_prompt = (
            "You are an expert tutor creating high-yield study flashcards from the provided document.\n"
            "Extract key concepts, definitions, equations, and principles into a clean JSON array of flashcard objects.\n"
            "Each object must have exactly these keys:\n"
            "- 'question': clear, direct prompt or term\n"
            "- 'answer': concise, accurate explanation\n"
            "- 'key_term': core subject term\n"
            "- 'page_ref': integer page number or null if unknown\n\n"
            "Output ONLY valid JSON inside a ```json ... ``` code fence, with no conversational commentary."
        )
        prompt = (
            f"DOCUMENT TITLE: {doc.get('filename')}\n\n"
            f"DOCUMENT CONTENT:\n{text[:25000]}\n\n"
            f"Generate {req.count} high-yield study flashcards in JSON format."
        )
    elif study_type == "quiz":
        sys_prompt = (
            "You are an assessment specialist creating a rigorous multiple-choice quiz from the provided document.\n"
            "Generate a clean JSON array of quiz question objects.\n"
            "Each object must have exactly these keys:\n"
            "- 'question': the problem or question statement\n"
            "- 'options': an array of exactly 4 strings (3 distractors and 1 correct answer)\n"
            "- 'correct_index': integer (0 to 3) representing the index of the correct option\n"
            "- 'explanation': rationale explaining why the correct answer is right\n"
            "- 'page_ref': integer page number or null\n\n"
            "Output ONLY valid JSON inside a ```json ... ``` code fence, with no conversational commentary."
        )
        prompt = (
            f"DOCUMENT TITLE: {doc.get('filename')}\n\n"
            f"DOCUMENT CONTENT:\n{text[:25000]}\n\n"
            f"Generate a {req.count}-question multiple choice quiz in JSON format."
        )
    elif study_type == "summary":
        sys_prompt = (
            "You are an executive technical analyst and research director.\n"
            "Create a comprehensive, beautifully structured executive summary and study guide from the provided document.\n"
            "Structure your report using the following markdown sections:\n"
            "# 📋 Executive Summary\n"
            "# 🔑 Key Takeaways (Bulleted list of core findings with [Page X] citations)\n"
            "# 🧠 Core Concepts & Methodologies Breakdown\n"
            "# 💡 Critical Analysis & Real-World Implications\n\n"
            "Use clear Markdown formatting and cite specific pages whenever possible."
        )
        prompt = (
            f"DOCUMENT TITLE: {doc.get('filename')}\n\n"
            f"DOCUMENT CONTENT:\n{text[:30000]}\n\n"
            "Produce the complete structured executive study guide now."
        )
    else:
        raise HTTPException(400, f"Unsupported study type: {study_type}")

    _eject_all_loaded()
    result = _run_model_completion(active_model, prompt, sys_prompt, 0.4)
    _eject_all_loaded()

    return {
        "ok": True,
        "study_type": study_type,
        "model": active_model,
        "doc_id": doc_id,
        "doc_title": doc.get("filename"),
        "result": result,
        "content": result
    }


@app.post("/api/documents/compare")
def compare_documents(req: DocumentCompareRequest, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    doc_a = db.get_library_document(req.id_a)
    doc_b = db.get_library_document(req.id_b)
    if not doc_a or not doc_b:
        raise HTTPException(404, "One or both documents not found")

    text_a = (doc_a.get("extracted_text") or "").strip()[:15000]
    text_b = (doc_b.get("extracted_text") or "").strip()[:15000]

    active_model = req.model or DEFAULT_MODELS.get("friend", "llama3.2:3b")
    sys_prompt = (
        "You are an analytical document comparison specialist.\n"
        "Perform a thorough comparative analysis between Document A and Document B.\n"
        "Structure your response with:\n"
        "# ⚖️ Document Comparison: Side-by-Side Analysis\n"
        "## 🤝 Common Ground & Shared Themes\n"
        "## ⚡ Key Differences & Contradictions\n"
        "## ➕ Unique Additions in Document B\n"
        "## ➖ Omissions / Discontinued Content from Document A\n"
        "## 🏆 Synthesis & Recommendation"
    )
    prompt = (
        f"--- DOCUMENT A ({doc_a.get('filename')}) ---\n{text_a}\n\n"
        f"--- DOCUMENT B ({doc_b.get('filename')}) ---\n{text_b}\n\n"
        "Provide your thorough comparative analysis now."
    )
    _eject_all_loaded()
    result = _run_model_completion(active_model, prompt, sys_prompt, 0.4)
    _eject_all_loaded()

    return {
        "ok": True,
        "doc_a": doc_a.get("filename"),
        "doc_b": doc_b.get("filename"),
        "model": active_model,
        "result": result,
        "comparison": result
    }


@app.post("/api/canvas/export_zip")
def canvas_export_zip(req: CanvasZipExportRequest, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    import io
    import zipfile

    buf = io.BytesIO()
    clean_title = re.sub(r"[^a-zA-Z0-9_.-]", "_", (req.filename or req.title or "web_project").replace(".zip", ""))

    file_map = {}
    if isinstance(req.files, list):
        for item in req.files:
            if isinstance(item, dict) and "name" in item:
                file_map[item["name"]] = item.get("content", "")
    elif isinstance(req.files, dict):
        file_map = req.files

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, content in file_map.items():
            safe_fname = Path(fname).name
            zf.writestr(safe_fname, str(content).encode("utf-8"))
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{clean_title}.zip"'}
    )


@app.post("/api/canvas/snapshots")
def save_canvas_snapshot(req: CanvasSnapshotRequest, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    art = db.save_canvas_artifact(
        title=req.title,
        lang=req.lang,
        code=req.code,
        parent_id=req.parent_id,
        version=req.version
    )
    return {"ok": True, "artifact": art, "snapshot_id": art["id"]}


@app.get("/api/canvas/snapshots")
def list_canvas_snapshots(limit: int = 50, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"snapshots": db.list_canvas_artifacts(limit=limit)}


@app.get("/api/canvas/snapshots/{aid}")
def get_canvas_snapshot(aid: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    art = db.get_canvas_artifact(aid)
    if not art:
        raise HTTPException(404, "Artifact snapshot not found")
    return {"artifact": art}


@app.delete("/api/canvas/snapshots/{aid}")
def delete_canvas_snapshot(aid: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"ok": db.delete_canvas_artifact(aid)}


# ==========================================================================
# Model Observatory & System Diagnostics (Phase D)
# ==========================================================================
def _get_gpu_telemetry() -> dict:
    import shutil
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {"available": False, "reason": "nvidia-smi not found"}
    try:
        cmd = [
            smi,
            "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        if res.returncode != 0 or not res.stdout.strip():
            return {"available": False, "reason": "nvidia-smi error"}
        line = res.stdout.strip().split("\n")[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 6:
            name = parts[0]
            total_mb = float(parts[1])
            used_mb = float(parts[2])
            free_mb = float(parts[3])
            util_pct = float(parts[4])
            temp_c = float(parts[5])
            used_pct = round((used_mb / total_mb) * 100, 1) if total_mb > 0 else 0.0
            return {
                "available": True,
                "name": name,
                "gpu_name": name,
                "vram_total_mb": round(total_mb, 1),
                "memory_total_mb": round(total_mb, 1),
                "vram_used_mb": round(used_mb, 1),
                "memory_used_mb": round(used_mb, 1),
                "vram_free_mb": round(free_mb, 1),
                "memory_free_mb": round(free_mb, 1),
                "vram_used_pct": used_pct,
                "memory_used_pct": used_pct,
                "gpu_util_pct": util_pct,
                "temperature_c": temp_c
            }
    except Exception as e:
        return {"available": False, "reason": str(e)}
    return {"available": False, "reason": "unknown"}


def _get_system_telemetry() -> dict:
    try:
        import psutil
        vm = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=None)
        return {
            "ram_total_gb": round(vm.total / (1024 ** 3), 2),
            "ram_used_gb": round(vm.used / (1024 ** 3), 2),
            "ram_free_gb": round(vm.available / (1024 ** 3), 2),
            "ram_used_pct": vm.percent,
            "cpu_pct": cpu,
            "cpu_util_pct": cpu
        }
    except Exception as e:
        return {
            "ram_total_gb": 0,
            "ram_used_gb": 0,
            "ram_free_gb": 0,
            "ram_used_pct": 0,
            "cpu_pct": 0,
            "cpu_util_pct": 0,
            "error": str(e)
        }


@app.get("/api/observatory/telemetry")
def observatory_telemetry(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    gpu = _get_gpu_telemetry()
    system = _get_system_telemetry()
    loaded_models = _get_loaded_models()
    stats = db.get_inference_stats()
    return {
        "ok": True,
        "gpu": gpu,
        "system": system,
        "loaded_models": loaded_models,
        "inference_kpi": stats,
        "stats": stats
    }


@app.get("/api/observatory/logs")
def observatory_logs(limit: int = 50, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"logs": db.get_inference_logs(limit=limit)}


@app.post("/api/observatory/clear_logs")
def observatory_clear_logs(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"ok": db.clear_inference_logs()}


class PromptTemplateIn(BaseModel):
    title: str
    user_template: str
    description: str = ""
    category: str = "General"
    system_prompt: str = ""
    is_favorite: int = 0
    id: str | None = None


@app.get("/api/prompt_templates")
def list_prompts(category: str | None = None, favorites_only: bool = False, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"templates": db.list_prompt_templates(category=category, favorites_only=favorites_only)}


@app.post("/api/prompt_templates")
def save_prompt(body: PromptTemplateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if not body.title.strip() or not body.user_template.strip():
        raise HTTPException(400, "Title and Template content are required")
    tmpl = db.save_prompt_template(
        title=body.title,
        user_template=body.user_template,
        description=body.description,
        category=body.category,
        system_prompt=body.system_prompt,
        is_favorite=body.is_favorite,
        template_id=body.id
    )
    return {"ok": True, "template": tmpl}


@app.get("/api/prompt_templates/{template_id}")
def get_prompt(template_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    tmpl = db.get_prompt_template(template_id)
    if not tmpl:
        raise HTTPException(404, "Prompt template not found")
    return {"template": tmpl}


@app.delete("/api/prompt_templates/{template_id}")
def delete_prompt(template_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"ok": db.delete_prompt_template(template_id)}


@app.post("/api/prompt_templates/{template_id}/favorite")
def toggle_prompt_fav(template_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    new_fav = db.toggle_prompt_favorite(template_id)
    return {"ok": True, "is_favorite": new_fav}


# ==============================================================================
# Phase 1: Project Workspaces API
# ==============================================================================

@app.get("/api/workspaces")
def workspaces_list(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"workspaces": db.list_workspaces()}


@app.get("/api/workspaces/{ws_id}")
def workspace_get(ws_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    ws = db.get_workspace(ws_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return {"workspace": ws}


@app.post("/api/workspaces")
def workspace_create(body: WorkspaceCreateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if not body.name.strip():
        raise HTTPException(400, "Workspace name cannot be empty")
    ws = db.create_workspace(
        name=body.name,
        slug=body.slug,
        description=body.description,
        instructions=body.instructions,
        icon=body.icon or "📁",
        color=body.color or "#38bdf8",
        repo_path=body.repo_path,
        model_pref=body.model_pref
    )
    return {"ok": True, "workspace": ws}


@app.put("/api/workspaces/{ws_id}")
def workspace_update(ws_id: str, body: WorkspaceUpdateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    ws = db.update_workspace(
        ws_id=ws_id,
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        icon=body.icon,
        color=body.color,
        repo_path=body.repo_path,
        model_pref=body.model_pref
    )
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return {"ok": True, "workspace": ws}


@app.delete("/api/workspaces/{ws_id}")
def workspace_delete(ws_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if ws_id == "ws_default":
        raise HTTPException(400, "Cannot delete the default workspace")
    ok = db.delete_workspace(ws_id)
    return {"ok": ok}


# ==============================================================================
# Phase 1: Unified Memory Control Center API
# ==============================================================================

@app.get("/api/memories")
def memories_list(workspace_id: str | None = None, scope: str | None = None,
                  category: str | None = None, search: str | None = None,
                  active_only: bool = False, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"memories": db.list_memories(
        workspace_id=workspace_id, scope=scope,
        category=category, search=search, active_only=active_only
    )}


@app.post("/api/memories")
def memory_create(body: MemoryCreateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if not body.content.strip():
        raise HTTPException(400, "Memory content cannot be empty")
    mem = db.create_memory(
        content=body.content,
        workspace_id=body.workspace_id,
        scope=body.scope,
        category=body.category,
        source_session_id=body.source_session_id,
        confidence=body.confidence
    )
    return {"ok": True, "memory": mem}


@app.put("/api/memories/{mem_id}")
def memory_update(mem_id: str, body: MemoryUpdateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    mem = db.update_memory(
        memory_id=mem_id,
        content=body.content,
        category=body.category,
        is_active=body.is_active,
        scope=body.scope,
        workspace_id=body.workspace_id
    )
    if not mem:
        raise HTTPException(404, "Memory not found")
    return {"ok": True, "memory": mem}


@app.delete("/api/memories/{mem_id}")
def memory_delete(mem_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"ok": db.delete_memory(mem_id)}


# ==============================================================================
# Phase 1: Context Inspector & Prompt Assembly API
# ==============================================================================

@app.post("/api/context/inspect")
def context_inspect(body: ContextInspectIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")

    mode = body.mode if body.mode in db.MODES else "architect"
    sid = body.session_id or db.get_or_create_latest_session(mode, workspace_id=body.workspace_id)
    active_skill, clean_msg = _extract_skill(body.skill, body.message)
    formatted_prompt, images_b64 = _format_user_prompt(clean_msg, [])

    model = body.model or _resolve_model(body)
    num_ctx = _get_num_ctx(model)
    caps = get_model_thinking_capabilities(model)
    supported_levels = caps.get("supported_levels", ["off"])
    eff_thinking_level = body.thinking_level if body.thinking_level in supported_levels else caps.get("default_level", "off")

    msgs, was_compacted = _build_messages(
        mode=mode,
        user_msg=formatted_prompt or None,
        session_id=sid,
        skill=active_skill,
        thinking_level=eff_thinking_level,
        supports_thinking=caps.get("supports_thinking", False),
        num_ctx=num_ctx,
        workspace_id=body.workspace_id,
        exclude_memories=body.exclude_memories,
        exclude_turns=body.exclude_turns
    )

    sess = db.get_session(sid) if sid else None
    eff_ws_id = body.workspace_id or (sess.get("workspace_id") if sess else "ws_default") or "ws_default"
    ws = db.get_workspace(eff_ws_id)
    all_ws_mems = db.get_relevant_memories_for_context(eff_ws_id, limit=30)
    exclude_mems_set = set(body.exclude_memories)

    past = db.history(mode, 60, session_id=sid)
    exclude_turns_set = set(body.exclude_turns)

    history_turns = []
    for idx, t in enumerate(past):
        txt = t.get("content") or ""
        history_turns.append({
            "index": idx,
            "role": t.get("role", "user"),
            "content": txt,
            "chars": len(txt),
            "est_tokens": max(1, len(txt) // 4),
            "included": idx not in exclude_turns_set
        })

    memories_list = []
    for m in all_ws_mems:
        c = m.get("content") or ""
        memories_list.append({
            "id": m["id"],
            "scope": m["scope"],
            "category": m["category"],
            "content": c,
            "chars": len(c),
            "est_tokens": max(1, len(c) // 4),
            "included": m["id"] not in exclude_mems_set
        })

    persona_text = ""
    if mode == "friend":
        persona_text = _friend_system(clean_msg)
    elif mode == "nsfw":
        persona_text = _nsfw_system()
    elif mode == "architect":
        persona_text = _architect_system(clean_msg, workspace_id=eff_ws_id)
    elif mode == "muse":
        persona_text = _muse_system(clean_msg)

    total_chars = sum(len(m.get("content", "")) for m in msgs)
    total_tokens = max(1, round(total_chars / 3.8))

    return {
        "ok": True,
        "mode": mode,
        "session_id": sid,
        "workspace": ws,
        "model": model,
        "context_limit": num_ctx,
        "total_chars": total_chars,
        "estimated_tokens": total_tokens,
        "was_compacted": was_compacted,
        "layers": {
            "workspace_instructions": {
                "name": ws["name"] if ws else "Default",
                "content": ws.get("instructions", "") if ws else "",
                "repo_path": ws.get("repo_path", "") if ws else "",
                "chars": len(ws.get("instructions", "")) if ws else 0,
                "est_tokens": max(1, len(ws.get("instructions", "")) // 4) if ws and ws.get("instructions") else 0
            },
            "system_persona": {
                "mode": mode,
                "content": persona_text,
                "chars": len(persona_text),
                "est_tokens": max(1, len(persona_text) // 4)
            },
            "memories": memories_list,
            "skill": {
                "name": active_skill.get("name") if active_skill else None,
                "command": active_skill.get("command") if active_skill else None,
                "prompt": active_skill.get("prompt") if active_skill else None
            } if active_skill else None,
            "thinking_level": {
                "level": eff_thinking_level,
                "supported": caps.get("supports_thinking", False)
            },
            "history_turns": history_turns,
            "user_prompt": {
                "content": formatted_prompt,
                "chars": len(formatted_prompt),
                "est_tokens": max(1, len(formatted_prompt) // 4)
            }
        },
        "raw_messages": msgs
    }


# ==============================================================================
# Phase 1: Database Backup Snapshots API
# ==============================================================================

@app.get("/api/backup/list")
def backup_list(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    return {"backups": db.list_backup_snapshots()}


@app.post("/api/backup/create")
def backup_create(x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    snap = db.create_backup_snapshot(trigger_type="manual")
    if not snap:
        raise HTTPException(500, "Backup creation failed")
    return {"ok": True, "snapshot": snap}


@app.delete("/api/backup/{snap_id}")
def backup_delete(snap_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    ok = db.delete_backup_snapshot(snap_id)
    return {"ok": ok}


@app.get("/api/backup/download/{snap_id}")
def backup_download(snap_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    snap = db.get_backup_snapshot(snap_id)
    if not snap or not Path(snap["filepath"]).exists():
        raise HTTPException(404, "Backup snapshot file not found")
    from fastapi.responses import FileResponse
    return FileResponse(
        path=snap["filepath"],
        filename=snap["filename"],
        media_type="application/x-sqlite3"
    )


# ==============================================================================
# Phase 2: Developer Workbench & Local Repository Intelligence API
# ==============================================================================

@app.get("/api/dev/status")
def dev_status(workspace_id: str | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    repo = dev_workbench.resolve_repo_path(workspace_id)
    if not repo:
        return {
            "ok": False,
            "error": "No repository configured or detected for this workspace."
        }
    status = dev_workbench.get_git_status(repo)
    return {"ok": True, "repo_path": str(repo), "status": status}


@app.get("/api/dev/diff")
def dev_diff(workspace_id: str | None = None, filepath: str | None = None,
             staged: bool = False, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    repo = dev_workbench.resolve_repo_path(workspace_id)
    if not repo:
        raise HTTPException(400, "No repository configured or detected.")
    diff_text = dev_workbench.get_git_diff(repo, filepath=filepath, staged=staged)
    return {"ok": True, "diff": diff_text}


@app.get("/api/dev/tree")
def dev_tree(workspace_id: str | None = None, subpath: str = "",
             x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    repo = dev_workbench.resolve_repo_path(workspace_id)
    if not repo:
        raise HTTPException(400, "No repository configured or detected.")
    tree = dev_workbench.get_file_tree(repo, subpath=subpath)
    return {"ok": True, "tree": tree, "repo_name": repo.name, "repo_path": str(repo)}


@app.get("/api/dev/file")
def dev_file(path: str, workspace_id: str | None = None,
             x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    repo = dev_workbench.resolve_repo_path(workspace_id)
    if not repo:
        raise HTTPException(400, "No repository configured or detected.")
    file_info = dev_workbench.read_repo_file(repo, path)
    if "error" in file_info:
        raise HTTPException(400, file_info["error"])
    return {"ok": True, "file": file_info}


@app.post("/api/dev/patch/apply")
def dev_patch_apply(body: DevPatchApplyIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    repo = dev_workbench.resolve_repo_path(body.workspace_id)
    if not repo:
        raise HTTPException(400, "No repository configured or detected.")
    res = dev_workbench.apply_patch(repo, body.patch)
    if not res.get("success"):
        raise HTTPException(400, res.get("error", "Failed to apply patch."))
    return {"ok": True, "result": res}


@app.post("/api/dev/patch/rollback")
def dev_patch_rollback(body: DevPatchRollbackIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    repo = dev_workbench.resolve_repo_path(body.workspace_id)
    if not repo:
        raise HTTPException(400, "No repository configured or detected.")
    res = dev_workbench.rollback_patch(repo, body.backup_id)
    if not res.get("success"):
        raise HTTPException(400, res.get("error", "Rollback failed."))
    return {"ok": True, "result": res}


@app.post("/api/dev/run")
def dev_command_run(body: DevCommandRunIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    repo = dev_workbench.resolve_repo_path(body.workspace_id)
    if not repo:
        raise HTTPException(400, "No repository configured or detected.")
    res = dev_workbench.execute_guarded_command(repo, body.command, body.timeout)
    return {"ok": True, "execution": res}


# ==============================================================================
# Phase 4: Local Knowledge Library, Reading Desk & Personal Learning Studio API
# ==============================================================================

@app.get("/api/knowledge/sources")
def knowledge_sources_list(workspace_id: str | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    sources = knowledge_library.list_watch_folders(workspace_id)
    return {"ok": True, "sources": sources}


@app.post("/api/knowledge/sources")
def knowledge_source_add(body: WatchFolderIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        source = knowledge_library.add_watch_folder(
            body.folder_path,
            body.name,
            body.workspace_id or "ws_default",
            body.recursive
        )
        return {"ok": True, "source": source}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/api/knowledge/sources/{source_id}")
def knowledge_source_delete(source_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    ok = knowledge_library.remove_watch_folder(source_id)
    return {"ok": ok}


@app.post("/api/knowledge/sync")
def knowledge_sync(source_id: str | None = None, workspace_id: str | None = None,
                   x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    if source_id:
        res = knowledge_library.sync_watch_folder(source_id)
        return {"ok": True, "result": res}
    else:
        results = knowledge_library.sync_all_watch_folders(workspace_id)
        return {"ok": True, "results": results}


@app.get("/api/knowledge/documents")
def knowledge_documents_list(workspace_id: str | None = None, limit: int = 100,
                             x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    docs = knowledge_library.list_knowledge_documents(workspace_id, limit)
    return {"ok": True, "documents": docs}


@app.get("/api/knowledge/search")
def knowledge_search(q: str, workspace_id: str | None = None, limit: int = 25,
                     x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    results = knowledge_library.search_knowledge_fts(q, workspace_id, limit)
    return {"ok": True, "query": q, "results": results}


@app.get("/api/knowledge/reading/{doc_id}")
def knowledge_reading_get(doc_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    data = knowledge_library.get_document_for_reading(doc_id)
    if not data:
        raise HTTPException(404, "Document not found")
    return {"ok": True, "document": data}


@app.post("/api/knowledge/reading/{doc_id}/progress")
def knowledge_reading_progress(doc_id: str, body: ReadingProgressIn,
                               x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    ok = knowledge_library.save_reading_progress(
        doc_id,
        body.workspace_id or "ws_default",
        body.progress_pct,
        body.last_position,
        body.bookmarks
    )
    return {"ok": ok}


@app.post("/api/knowledge/reading/{doc_id}/ask")
def knowledge_reading_ask(doc_id: str, body: ReadingAskIn,
                          x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    doc_info = knowledge_library.get_document_for_reading(doc_id)
    doc_title = doc_info["title"] if doc_info else "the document"

    active_model = body.model or DEFAULT_MODELS.get("friend", "llama3.2:3b")
    sys_prompt = (
        f"You are the AI Reading Companion for \"{doc_title}\".\n"
        "Provide a direct, insightful, and precise explanation of the selected passage in the context of the work.\n"
        "Ground your answer strictly in the text, clarify ambiguous concepts, and distinguish explicit claims from interpretation."
    )
    prompt = (
        f"DOCUMENT: {doc_title}\n\n"
        f"SELECTED PASSAGE:\n\"{body.passage}\"\n\n"
        f"SURROUNDING CONTEXT:\n{body.context[:3000]}\n\n"
        f"READER'S QUESTION:\n{body.question}"
    )
    explanation = _run_model_completion(active_model, prompt, sys_prompt, 0.3)
    return {"ok": True, "explanation": explanation, "model": active_model}


@app.get("/api/knowledge/notes")
def knowledge_notes_list(workspace_id: str | None = None, doc_id: str | None = None,
                         x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    notes = knowledge_library.list_research_notes(workspace_id, doc_id)
    return {"ok": True, "notes": notes}


@app.post("/api/knowledge/notes")
def knowledge_note_add(body: ResearchNoteIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    note = knowledge_library.add_research_note(
        body.workspace_id or "ws_default",
        body.doc_id,
        body.doc_title,
        body.passage,
        body.note,
        body.tags,
        body.citation
    )
    return {"ok": True, "note": note}


@app.delete("/api/knowledge/notes/{note_id}")
def knowledge_note_delete(note_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    ok = knowledge_library.delete_research_note(note_id)
    return {"ok": ok}


@app.get("/api/knowledge/notes/export")
def knowledge_notes_export(workspace_id: str | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    md = knowledge_library.export_research_notes_markdown(workspace_id)
    return {"ok": True, "markdown": md}


@app.get("/api/study/flashcards")
def study_flashcards_list(workspace_id: str | None = None, deck_id: str | None = None,
                          due_only: bool = False, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    cards = knowledge_library.list_flashcards(workspace_id, deck_id, due_only)
    return {"ok": True, "flashcards": cards}


@app.get("/api/study/decks")
def study_decks_list(workspace_id: str | None = None, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    decks = knowledge_library.list_flashcard_decks(workspace_id)
    return {"ok": True, "decks": decks}


@app.post("/api/study/flashcards")
def study_flashcard_create(body: FlashcardCreateIn, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    card = knowledge_library.create_flashcard(
        body.deck_id,
        body.deck_title,
        body.workspace_id or "ws_default",
        body.front,
        body.back
    )
    return {"ok": True, "flashcard": card}


@app.post("/api/study/flashcards/{card_id}/review")
def study_flashcard_review(card_id: str, body: FlashcardReviewIn,
                           x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    try:
        res = knowledge_library.review_flashcard_sm2(card_id, body.rating)
        return {"ok": True, "result": res}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/api/study/flashcards/{card_id}")
def study_flashcard_delete(card_id: str, x_passcode: str | None = Header(default=None)):
    if not _passcode_ok(x_passcode):
        raise HTTPException(423, "locked — unlock with passcode")
    ok = knowledge_library.delete_flashcard(card_id)
    return {"ok": ok}




