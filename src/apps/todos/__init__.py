"""Package entry point.

`main()` is the console-script target declared in `pyproject.toml` under
`[project.scripts]`. The tracer bullet wires a runnable harness so the script
target actually starts the server; production deployment (process model,
env-var policy, file-backed DB) lands in a follow-up ticket.
"""

from __future__ import annotations

import uvicorn


def main() -> None:
    """Run the todos HTTP service via uvicorn. Dev harness only."""
    from .app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)