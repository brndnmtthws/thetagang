from __future__ import annotations

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


class Config(BaseModel):
    """Top-level configuration model used by the CLI."""

    model_config = ConfigDict(extra="ignore")

    account: AccountConfig
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    ignored_sections: List[str] = Field(default_factory=list, exclude=True)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        """Create a :class:`Config` from the raw TOML payload."""

        recognized_keys = {"account", "connection"}
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

        panel_title = f"Configuration ({source})"
        console.print(Panel(table, title=panel_title, expand=False))

        if self.ignored_sections:
            ignored = ", ".join(self.ignored_sections)
            console.print(
                "[yellow]Ignored configuration sections:[/yellow] "
                f"{ignored}"
            )
