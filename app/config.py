import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    generation_store_path: str
    llm_provider: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_max_attempts: int
    ocr_crosscheck: bool


def get_settings() -> Settings:
    _load_dotenv()
    (ROOT / "var").mkdir(exist_ok=True)
    return Settings(
        database_url=os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'var' / 'ct200.db'}"),
        generation_store_path=os.getenv("GENERATION_STORE_PATH", str(ROOT / "var" / "generations.json")),
        llm_provider=os.getenv("LLM_PROVIDER", "mock"),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        llm_model=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_max_attempts=int(os.getenv("LLM_MAX_ATTEMPTS", "3")),
        ocr_crosscheck=os.getenv("OCR_CROSSCHECK", "0") == "1",
    )
