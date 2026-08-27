"""Lets the agent run as ``python -m vmcp.agent`` from a source checkout.

The deployed zipapp uses its own generated ``__main__`` at the archive root
(see ``server/transport/payload.py``), so this path is for development only.
"""

from .main import main

raise SystemExit(main())
