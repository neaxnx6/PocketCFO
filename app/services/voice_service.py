from openai import AsyncOpenAI
from app.config import settings

client = AsyncOpenAI(
    api_key=settings.PROXY_API_KEY, 
    base_url=settings.PROXY_API_BASE
)

async def transcribe_voice(file_path: str) -> str:
    """
    Отправляет аудио-файл в OpenAI-совместимый Whisper API (VseGPT/ProxyAPI).
    """
    with open(file_path, "rb") as audio_file:
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file
        )
        return transcript.text

