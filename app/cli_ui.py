"""Rich CLI interface for SpaceRouter Node.

Provides:
- Interactive setup wizard with styled prompts and selection menus
- Live status dashboard that updates in place while the node runs
"""

import logging
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)

console = Console()


# ---------------------------------------------------------------------------
# Setup wizard (rich prompts)
# ---------------------------------------------------------------------------

def wizard_banner() -> None:
    console.print()
    console.print(Panel.fit(
        "[bold cyan]SpaceRouter Node — Setup[/]",
        border_style="cyan",
    ))
    console.print()


def wizard_step(number: int, title: str) -> None:
    console.print(f"  [bold yellow]{number}.[/] [bold]{title}[/]")


def wizard_select(prompt: str, choices: list[tuple[str, str]], default: int = 0) -> int:
    """Arrow-key style selection menu. Returns the index of the chosen item.

    Each choice is (label, description). Renders as numbered list and accepts
    the number input.
    """
    for i, (label, desc) in enumerate(choices):
        marker = "[bold green]→[/]" if i == default else " "
        num = f"[bold]{i + 1}[/]"
        console.print(f"     {marker} {num}  {label}  [dim]{desc}[/]")
    console.print()
    while True:
        raw = Prompt.ask(
            f"     [bold]Select[/]",
            default=str(default + 1),
            console=console,
        )
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                return idx
        except ValueError:
            pass
        console.print(f"     [red]Please enter a number between 1 and {len(choices)}[/]")


def wizard_input(prompt: str, default: str = "", password: bool = False) -> str:
    return Prompt.ask(
        f"     {prompt}",
        default=default or None,
        password=password,
        console=console,
    ) or ""


def wizard_confirm(prompt: str, default: bool = False) -> bool:
    return Confirm.ask(f"     {prompt}", default=default, console=console)


def wizard_success(msg: str) -> None:
    console.print(f"     [green]✓[/] {msg}")


def wizard_error(msg: str) -> None:
    console.print(f"     [red]✗[/] {msg}")


def wizard_info(msg: str) -> None:
    console.print(f"     [dim]{msg}[/]")


def wizard_done(env_path: str) -> None:
    console.print()
    console.print(Panel.fit(
        f"[bold green]Configuration saved to {env_path}[/]\n"
        "[dim]Starting node...[/]",
        border_style="green",
    ))
    console.print()


# ---------------------------------------------------------------------------
# Live status dashboard
# ---------------------------------------------------------------------------

class StatusDashboard:
    """Live-updating status panel for the running node.

    Replaces scrolling log output with a persistent display that
    updates in place, similar to htop or docker stats.
    """

    def __init__(self) -> None:
        self.node_id: str = ""
        self.state: str = "starting"
        self.staking_address: str = ""
        self.public_ip: str = ""
        self.port: int = 9090
        self.upnp: bool = False
        self.health_score: str = "—"
        self.health_status: str = "—"
        self.staking_status: str = "—"
        self.connections_served: int = 0
        self.connections_active: int = 0
        self.last_health_check: float = 0
        self.last_probe_result: str = "—"
        self.last_probe_time: float = 0
        self.uptime_start: float = time.time()
        self.errors: list[str] = []
        self.version: str = ""

        self._live: Live | None = None
        self._console = Console()

    def start(self) -> None:
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=1,
            transient=False,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def update(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
        if self._live:
            self._live.update(self._render())

    def log(self, message: str, style: str = "") -> None:
        """Print a log line below the dashboard."""
        if self._live:
            self._live.console.print(
                f"[dim]{time.strftime('%H:%M:%S')}[/] {f'[{style}]' if style else ''}{message}"
                f"{'[/]' if style else ''}"
            )

    def _uptime_str(self) -> str:
        elapsed = time.time() - self.uptime_start
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def _state_style(self) -> tuple[str, str]:
        """Return (display_text, rich_style) for current state."""
        styles = {
            "starting": ("STARTING", "yellow"),
            "initializing": ("INITIALIZING", "yellow"),
            "binding": ("BINDING", "yellow"),
            "registering": ("REGISTERING", "yellow"),
            "running": ("RUNNING", "bold green"),
            "reconnecting": ("RECONNECTING", "bold yellow"),
            "error_transient": ("ERROR (retrying)", "bold red"),
            "error_permanent": ("ERROR", "bold red"),
            "stopping": ("STOPPING", "dim"),
        }
        text, style = styles.get(self.state, (self.state.upper(), "white"))
        return text, style

    def _render(self) -> Panel:
        state_text, state_style = self._state_style()

        # Main status table
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("key", style="bold", width=20)
        table.add_column("value")

        table.add_row("Status", Text(state_text, style=state_style))
        table.add_row("Node ID", self.node_id[:16] + "..." if self.node_id else "—")
        table.add_row("Staking", self.staking_address[:16] + "..." if self.staking_address else "—")
        table.add_row("Endpoint", f"{self.public_ip}:{self.port}" if self.public_ip else "—")
        table.add_row("Network", "UPnP" if self.upnp else "Tunnel / Manual")
        table.add_row("Uptime", self._uptime_str())
        table.add_row("", "")  # spacer
        table.add_row("Connections", f"{self.connections_served} served ({self.connections_active} active)")
        table.add_row("Health", self._health_display())
        table.add_row("Staking Status", self._staking_display())
        table.add_row("Self-Probe", self._probe_display())

        if self.errors:
            table.add_row("", "")
            table.add_row("Last Error", Text(self.errors[-1], style="red"))

        title = f"SpaceRouter Node {self.version}" if self.version else "SpaceRouter Node"
        return Panel(
            table,
            title=f"[bold cyan]{title}[/]",
            subtitle="[dim]Ctrl+C to stop[/]",
            border_style="cyan" if self.state == "running" else "yellow",
        )

    def _health_display(self) -> Text:
        status = self.health_status
        score = self.health_score
        if status == "—" and self.last_probe_time == 0:
            return Text("waiting...", style="dim")
        # Prefer self-probe time (runs every 60s) over health check time (5min)
        check_time = self.last_probe_time or self.last_health_check
        ago = int(time.time() - check_time) if check_time else 0
        label = f"{status} (score: {score})" if score != "—" else status
        if status in ("online", "active"):
            return Text(f"● {label}  [{ago}s ago]", style="green")
        elif status in ("error", "unknown"):
            return Text(f"● {label}  [{ago}s ago]", style="red")
        return Text(f"● {label}  [{ago}s ago]", style="yellow")

    def _staking_display(self) -> Text:
        s = self.staking_status
        if s == "earning":
            return Text(f"● {s}", style="green")
        elif s == "qualifying":
            return Text(f"● {s}", style="yellow")
        elif s in ("inactive", "—"):
            return Text(f"● {s}", style="dim")
        return Text(f"● {s}", style="white")

    def _probe_display(self) -> Text:
        if self.last_probe_time == 0:
            return Text("pending...", style="dim")
        ago = int(time.time() - self.last_probe_time)
        result = self.last_probe_result
        if result in ("reachable", "ok", "online"):
            return Text(f"● reachable ({ago}s ago)", style="green")
        elif result == "pending":
            return Text(f"● probe sent ({ago}s ago)", style="yellow")
        return Text(f"● {result} ({ago}s ago)", style="red")
