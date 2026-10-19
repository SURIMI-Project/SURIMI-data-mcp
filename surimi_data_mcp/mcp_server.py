"""The FastMCP instance that every tool module registers on, and the `log()` function for server messages."""
from fastmcp import FastMCP

mcp = FastMCP("SURIMI Fisheries & OECD Data")


def log(msg: str) -> None:
    """Print `msg` to stdout with the `[surimi-mcp]` prefix."""
    print(f"[surimi-mcp] {msg}", flush=True)
