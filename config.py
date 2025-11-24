"""
VoiceDrop Configuration
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project directory
load_dotenv(Path(__file__).parent / ".env")

# ElevenLabs API Configuration
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

# Hotkey configuration (default: Ctrl + Shift + Space)
HOTKEY_MODIFIER = "ctrl+shift"
HOTKEY_KEY = "space"

# Audio settings
SAMPLE_RATE = 16000  # 16kHz - optimal for speech recognition
CHANNELS = 1  # Mono

# Database settings
DB_PATH = Path(__file__).parent / "voicedrop.db"
HISTORY_RETENTION_HOURS = 24

# API endpoints
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# Application settings
APP_NAME = "VoiceDrop"
TRAY_TOOLTIP = "VoiceDrop - Voice to Text (Ctrl+Shift+Space)"
