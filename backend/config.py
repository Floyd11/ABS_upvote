from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # Database
    database_url: str
    db_schema: str = "upvote_bot"

    # Encryption
    encryption_key: str

    # Abstract chain
    abstract_rpc_url: str = "https://api.mainnet.abs.xyz"
    voting_contract: str = "0x3B50dE27506f0a8C1f4122A1e6F470009a76ce2A"
    chain_id: int = 2741

    # Epoch
    epoch_zero_timestamp: int = 1730421821
    epoch_duration_seconds: int = 604800

    # API & Auth
    api_secret_key: str  # JWT подпись + admin X-Api-Key

    # Shared secret между FastAPI и tx-service (localhost only)
    # Генерируй: python -c "import secrets; print(secrets.token_hex(32))"
    tx_service_secret: str
    tx_service_url: str = "http://127.0.0.1:3010"

    # CORS
    cors_origins: str = "http://localhost:3000"

    # Scheduler
    scheduler_timezone: str = "UTC"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",")]

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
