from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUANTUM_BFF_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    operator_url: str = "http://127.0.0.1:8321"
    operator_token: SecretStr = SecretStr("")
    request_timeout: float = Field(default=30, gt=0, le=120)
    submit_timeout: float = Field(default=3750, gt=0, le=7200)
    read_only: bool = False
    frontend_dir: Path = Path(__file__).resolve().parents[3] / "frontend"

    @model_validator(mode="after")
    def operator_endpoint(self):
        try:
            url = urlsplit(self.operator_url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username is not None or url.password is not None
                    or url.path not in {"", "/"} or url.query or url.fragment
                    or any(c in self.operator_url for c in "\\%?#")
                    or any(ord(c) <= 32 or ord(c) == 127 for c in self.operator_url)
                    or url.port == 0
                    or (url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"})):
                raise ValueError
        except ValueError:
            raise ValueError("World operator URL must be a fixed HTTPS origin or loopback HTTP origin") from None
        token = self.operator_token.get_secret_value()
        if token and (len(token) < 32 or any(not 32 < ord(c) < 127 for c in token)):
            raise ValueError("World operator token must contain at least 32 printable non-space characters")
        return self
