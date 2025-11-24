"""
ElevenLabs API Client - Speech to Text
"""
import httpx
from typing import Optional

from config import ELEVENLABS_API_KEY, ELEVENLABS_STT_URL


class ElevenLabsClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or ELEVENLABS_API_KEY
        self._client: Optional[httpx.Client] = None

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client with connection pooling"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=5)
            )
        return self._client

    def transcribe(self, audio_data: bytes, language: str = None) -> str:
        """
        Transcribe audio to text using ElevenLabs API

        Args:
            audio_data: WAV audio data as bytes
            language: Language code (None = auto-detect)

        Returns:
            Transcribed text
        """
        if not self.api_key:
            raise ValueError("ElevenLabs API key is not configured")

        client = self._get_client()

        headers = {
            "xi-api-key": self.api_key,
        }

        files = {
            "file": ("audio.wav", audio_data, "audio/wav"),
        }

        data = {
            "model_id": "scribe_v1",
        }

        # Only add language_code if specified (otherwise auto-detect)
        if language:
            data["language_code"] = language

        response = client.post(
            ELEVENLABS_STT_URL,
            headers=headers,
            files=files,
            data=data
        )

        if response.status_code != 200:
            error_msg = f"ElevenLabs API error: {response.status_code}"
            try:
                error_data = response.json()
                if "detail" in error_data:
                    error_msg += f" - {error_data['detail']}"
            except Exception:
                error_msg += f" - {response.text}"
            raise Exception(error_msg)

        result = response.json()
        return result.get("text", "")

    async def transcribe_async(self, audio_data: bytes, language: str = "ru") -> str:
        """Async version of transcribe"""
        if not self.api_key:
            raise ValueError("ElevenLabs API key is not configured")

        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {
                "xi-api-key": self.api_key,
            }

            files = {
                "file": ("audio.wav", audio_data, "audio/wav"),
            }

            data = {
                "model_id": "scribe_v1",
                "language_code": language,
            }

            response = await client.post(
                ELEVENLABS_STT_URL,
                headers=headers,
                files=files,
                data=data
            )

            if response.status_code != 200:
                raise Exception(f"ElevenLabs API error: {response.status_code} - {response.text}")

            result = response.json()
            return result.get("text", "")

    def close(self):
        """Close the HTTP client"""
        if self._client and not self._client.is_closed:
            self._client.close()
