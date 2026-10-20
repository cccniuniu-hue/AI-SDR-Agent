import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    thinking: str = "disabled"
    search_enabled: bool = True
    database_path: str = ".data/sdr.sqlite3"
    timeout: float = 30.0
    analysis_timeout: float = 180.0
    company_timeout: float = 30.0

    def __post_init__(self):
        if not math.isfinite(self.company_timeout) or self.company_timeout <= 0:
            raise ValueError("COMPANY_TIMEOUT_SECONDS 必须是大于0的有限秒数")

    @classmethod
    def from_env(cls, env_file: str | Path = ".env"):
        load_dotenv(env_file, override=False)
        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            thinking=os.getenv("DEEPSEEK_THINKING", "disabled"),
            search_enabled=os.getenv("COMPANY_SEARCH_ENABLED", "true").lower() == "true",
            database_path=os.getenv("DATABASE_PATH", ".data/sdr.sqlite3"),
            company_timeout=float(os.getenv("COMPANY_TIMEOUT_SECONDS", "30")),
        )
