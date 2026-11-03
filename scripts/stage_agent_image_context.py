#!/usr/bin/env python3
"""Stage the reproducible `agent-langgraph-agui` image build context.

Layout mirrors the upstream repo paths the pinned Dockerfile COPYs, so
`docker build` runs the exact upstream recipe against hash-verified
vendored sources (see src/me_runtime/upstream/vendor/VENDOR.md):

    <dest>/Dockerfile                              (pinned upstream recipe)
    <dest>/agent-langgraph-agui/requirements.txt   (upstream requirement set)
    <dest>/agent-langgraph-agui/src/*.py           (vendored, hash-verified)
    <dest>/shared/model_providers.py + model-providers.json
    <dest>/constraints-me.txt                      (coherent resolved pins)

Usage:
    python scripts/stage_agent_image_context.py /tmp/agent-ctx
    docker build -t openbot-agent-langgraph-agui:<pin12> /tmp/agent-ctx

Stdlib only. Verifies the staged context before printing the manifest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from me_runtime.agent_service import (  # noqa: E402
    AGENT_PIN,
    build_context,
    docker_run_args,
    verify_build_context,
)


def main(argv: list[str]) -> int:
    from me_runtime.agent_service import AGENT_PIN as _PIN
    if len(argv) != 2:
        print(f"usage: {argv[0]} <dest-dir>", file=sys.stderr)
        return 2
    dest = Path(argv[1])
    # Never follow a symlink destination or take over a foreign nonempty
    # directory (it could hold an arbitrary Dockerfile). Re-running over
    # our own staged context (ownership marker present) is safe; no
    # destructive cleanup is ever performed.
    if dest.is_symlink():
        print(f"ERROR: refusing symlink destination: {dest}", file=sys.stderr)
        return 1
    if dest.is_dir() and any(dest.iterdir()):
        marker = dest / ".me-owned-stage"
        if not marker.is_file() or marker.read_text().strip() != (
                f"openbot-agent-langgraph-agui:{_PIN}"):
            print(f"ERROR: refusing foreign nonempty directory (no "
                  f"ownership marker): {dest}", file=sys.stderr)
            return 1
    manifest = build_context(argv[1])
    problems = verify_build_context(argv[1])
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"docker build -t openbot-agent-langgraph-agui:{AGENT_PIN[:12]} "
          f"{manifest['dest']}")
    print("run: " + " ".join(
        docker_run_args(f"openbot-agent-langgraph-agui:{AGENT_PIN[:12]}",
                        "<env-file>")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
