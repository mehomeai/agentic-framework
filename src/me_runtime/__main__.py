"""Allow `python -m me_runtime.bridge` and `python -m me_runtime`."""
import os as _os

_os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from .bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
