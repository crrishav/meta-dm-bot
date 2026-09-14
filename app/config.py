import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v25.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
GRAPH_IG = f"https://graph.instagram.com/{GRAPH_VERSION}"

APP_SECRET = _required("META_APP_SECRET")
IG_APP_SECRET = _required("META_IG_APP_SECRET")
VERIFY_TOKEN = _required("META_VERIFY_TOKEN")
IG_TOKEN = _required("META_IG_TOKEN")

GOOGLE_API_KEY = _required("GOOGLE_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

DEBOUNCE_SECONDS = float(os.environ.get("DEBOUNCE_SECONDS", "2"))
HANDOFF_HOURS = float(os.environ.get("HANDOFF_HOURS", "12"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "20"))
DB_PATH = os.environ.get("DB_PATH", "bot.db")

SUPABASE_URL = _required("SUPABASE_URL")
SUPABASE_SERVICE_KEY = _required("SUPABASE_SERVICE_KEY")

# The bot's brief lives in a markdown file so non-engineers can edit it.
PERSONA = Path(os.environ.get("PERSONA_FILE", "persona.md")).read_text(encoding="utf-8")

# The model emits this token when it wants a human to take the conversation.
HANDOFF_MARKER = "<<HANDOFF>>"
