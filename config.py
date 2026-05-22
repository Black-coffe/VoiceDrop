"""
VoiceDrop Configuration
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

def get_base_path():
    """Get base path that works for both development and PyInstaller"""
    if getattr(sys, 'frozen', False):
        # Running as compiled exe
        return Path(sys.executable).parent
    else:
        # Running in development
        return Path(__file__).parent

# Get base directory
BASE_DIR = get_base_path()

# Load .env file from base directory
load_dotenv(BASE_DIR / ".env")

# ElevenLabs API Configuration
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

# Anthropic API — for optional LLM text polishing (filler removal, punctuation)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
POLISH_MODEL = "claude-haiku-4-5"

# Hotkey configuration (default: Ctrl + Shift + Space)
HOTKEY_MODIFIER = "ctrl+shift"
HOTKEY_KEY = "space"

# Audio settings
SAMPLE_RATE = 16000  # 16kHz - optimal for speech recognition
CHANNELS = 1  # Mono

# Database settings
DB_PATH = BASE_DIR / "voicedrop.db"
HISTORY_RETENTION_HOURS = 24

# API endpoints
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# Application settings
APP_NAME = "VoiceDrop"
TRAY_TOOLTIP = "VoiceDrop - Voice to Text (Ctrl+Shift+Space)"
