from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


class AccountConfig(BaseModel):
    """Configuration values that describe the IBKR account to query."""

    number: str = Field(..., description="IBKR account number")
    market_data_type: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Market data type as defined by the TWS API",
    )


class ConnectionConfig(BaseModel):
    """Connection settings for an existing TWS or IB Gateway session."""

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=7497)
    client_id: int = Field(default=1, ge=0)


def _default_logfile_path() -> Path:
    """Return the default location for the ib_async log file."""

    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        base_dir = Path(state_home)
    else:
        base_dir = Path.home() / ".local" / "state"
    return base_dir / "thetagang" / "ib_async.log"


class IBAsyncConfig(BaseModel):
    """Settings controlling ib_async's logging output."""

    logfile: Path = Field(default_factory=_default_logfile_path)

    def resolve_logfile(self) -> Path:
        """Return a writable logfile path, falling back when necessary."""

        primary = self.logfile.expanduser()
        default_path = _default_logfile_path()
        candidates = [primary]
        if primary != default_path:
            candidates.append(default_path)

        for candidate in candidates:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            return candidate

        raise OSError(
            f"Unable to prepare a directory for the ib_async logfile: {primary}"
        )


class Config(BaseModel):
    """Top-level configuration model used by the CLI."""

    model_config = ConfigDict(extra="ignore")

    account: AccountConfig
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    ib_async: IBAsyncConfig = Field(default_factory=IBAsyncConfig)
    ignored_sections: List[str] = Field(default_factory=list, exclude=True)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        """Create a :class:`Config` from the raw TOML payload."""

        recognized_keys = {"account", "connection", "ib_async"}
        relevant_data = {
            key: raw[key]
            for key in recognized_keys
            if key in raw and isinstance(raw[key], dict)
        }

        config = cls.model_validate(relevant_data)
        config.ignored_sections = sorted(
            key for key in raw.keys() if key not in recognized_keys
        )
        return config

    def display(self, source: str) -> None:
        """Render the loaded configuration to the console."""

        table = Table(show_header=False)
        table.add_row("Account number", self.account.number)
        table.add_row("Market data type", str(self.account.market_data_type))
        table.add_section()
        table.add_row("Host", self.connection.host)
        table.add_row("Port", str(self.connection.port))
        table.add_row("Client ID", str(self.connection.client_id))
        table.add_section()
        table.add_row("ib_async log", str(self.ib_async.logfile))

        panel_title = f"Configuration ({source})"
        console.print(Panel(table, title=panel_title, expand=False))

        if self.ignored_sections:
            ignored = ", ".join(self.ignored_sections)
            console.print(
                "[yellow]Ignored configuration sections:[/yellow] "
                f"{ignored}"
            )
