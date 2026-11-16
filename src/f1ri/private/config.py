"""Validated operator configuration. No browser request sets a path or an issuer."""

from os import environ
from pathlib import Path
from re import fullmatch
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class PrivateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    environment: str = Field(pattern=r"^(staging|production)$")
    origin: str
    oidc_issuer: str
    oidc_client_id: str = Field(min_length=1, max_length=128)
    oidc_client_secret: SecretStr
    flow_secret: SecretStr
    edge_secret: SecretStr
    database: Path
    issuer_ca: Path | None = None
    session_seconds: int = Field(1800, ge=15, le=14400)
    queue_seconds: int = Field(90, ge=10, le=120)
    owner_pending: int = Field(2, ge=1, le=3)
    workspace_limit: int = Field(50, ge=1, le=100)
    rate_submissions: int = Field(20, ge=1, le=30)
    rate_exports: int = Field(12, ge=1, le=20)
    result_days: int = Field(7, ge=1, le=30)
    log_level: str = Field("INFO", pattern=r"^(INFO|WARNING)$")

    @model_validator(mode="after")
    def verify(self):
        for url in (self.origin, self.oidc_issuer):
            value = urlsplit(url)
            if (value.scheme != "https" or not value.hostname or value.username or value.password or
                    url.endswith("/") or value.fragment or value.query or " " in url):
                raise ValueError("Use canonical HTTPS origins and issuer URLs.")
        if urlsplit(self.origin).path or not urlsplit(self.oidc_issuer).path:
            raise ValueError("The application origin has no path and the OIDC issuer has a realm path.")
        if not self.database.is_absolute():
            raise ValueError("The operator database path must be absolute.")
        if self.issuer_ca and (not self.issuer_ca.is_absolute() or not self.issuer_ca.is_file()):
            raise ValueError("The optional OIDC CA must be a readable absolute path.")
        if self.environment == "staging" and any(urlsplit(x).hostname not in ("localhost", "127.0.0.1", "::1")
                                                 for x in (self.origin, self.oidc_issuer)):
            raise ValueError("The staging origins must be loopback addresses.")
        if self.environment == "production" and self.session_seconds < 300:
            raise ValueError("The production session limit is at least five minutes.")
        values = [x.get_secret_value() for x in (self.oidc_client_secret, self.flow_secret, self.edge_secret)]
        if len(set(values)) != 3 or any(len(value) < 32 or not value.isascii() or not value.isprintable()
                                        for value in values):
            raise ValueError("Configure three independent secrets of at least 32 characters.")
        if not fullmatch(r"[A-Za-z0-9_-]{32,128}", values[-1]):
            raise ValueError("The gateway secret must be URL-safe.")
        return self

    @property
    def host(self):
        return urlsplit(self.origin).hostname


def configured():
    path = environ.get("F1RI_PRIVATE_CONFIG")
    if path:
        if not Path(path).is_absolute():
            raise ValueError("Use an absolute F1RI_PRIVATE_CONFIG path.")
        return PrivateConfig.model_validate_json(Path(path).read_bytes())
    return None
