"""Animation adapter on Pillow (maintained imaging engine).

Produces an editable `scene.py` (stdlib + Pillow, title/caption as data
only) plus a REAL animated GIF rendered in-process from a fixed template,
and an HTML preview file that references the GIF relatively. A preview
file is a workspace artifact for download — it must never be served
inline on the authenticated home origin. MP4 export additionally needs
ffmpeg; when absent the GIF remains the authoritative artifact and the
missing prerequisite is reported, never faked.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any

from langchain_core.tools import tool

from ..errors import ToolDeniedError
from ..workspace import Workspace

WIDTH, HEIGHT = 640, 360


def _esc(s: str) -> str:
    return ((s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")[:160])


def render_gif(title: str, caption: str, frames: int) -> bytes:
    """Render the animation with Pillow; raises ToolDeniedError if missing."""
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # noqa: BLE001
        raise ToolDeniedError(
            "prerequisite_missing",
            f"Pillow render engine not installed: {exc}") from exc
    import io
    import math

    frames = max(2, min(int(frames or 6), 24))
    imgs = []
    for i in range(frames):
        t = i / max(frames - 1, 1)
        img = Image.new("RGB", (WIDTH, HEIGHT), (16, 24, 40))
        d = ImageDraw.Draw(img)
        x = 60 + t * (WIDTH - 120)
        y = HEIGHT / 2 + math.sin(t * math.pi * 2) * 60
        d.ellipse([x - 26, y - 26, x + 26, y + 26],
                  fill=(int(90 + 120 * t) % 256, 140, 220))
        d.text((24, 16), f"{title[:60]} ({i + 1}/{frames})",
               fill=(255, 255, 255))
        d.text((24, HEIGHT - 24), caption[:80], fill=(203, 213, 225))
        imgs.append(img)
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=500, loop=0)
    return buf.getvalue()


SCENE_TEMPLATE = '''"""Editable animation source (stdlib + Pillow).

Regenerate:  python scene.py "Title" "Caption"   # writes animation.gif
Requires Pillow (`pip install Pillow`). Title/caption are DATA ONLY.
"""
import math
import sys

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 640, 360


def render(title, caption, frames=6):
    imgs = []
    for i in range(frames):
        t = i / max(frames - 1, 1)
        img = Image.new("RGB", (WIDTH, HEIGHT), (16, 24, 40))
        d = ImageDraw.Draw(img)
        x = 60 + t * (WIDTH - 120)
        y = HEIGHT / 2 + math.sin(t * math.pi * 2) * 60
        d.ellipse([x - 26, y - 26, x + 26, y + 26],
                  fill=(int(90 + 120 * t) % 256, 140, 220))
        d.text((24, 16), f"{title[:60]} ({i + 1}/{frames})",
               fill=(255, 255, 255))
        d.text((24, HEIGHT - 24), caption[:80], fill=(203, 213, 225))
        imgs.append(img)
    imgs[0].save("animation.gif", save_all=True, append_images=imgs[1:],
                 duration=500, loop=0)


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else "untitled",
           sys.argv[2] if len(sys.argv) > 2 else "")
'''


def make_animate_tools(ws: Workspace, capabilities: dict,
                       record_artifact) -> list[Any]:
    @tool("animation_build")
    def animation_build(title: str, caption: str = "") -> str:
        """Build an animated GIF + editable scene.py. Args: title, caption."""
        if not capabilities.get("animation"):
            raise ToolDeniedError("capability_denied", "animation not granted")
        title = (title or "untitled")[:120]
        gif = render_gif(title, caption or "", 6)
        target = ws.write_bytes("output/animation/animation.gif", gif)
        record_artifact(ws.relative(target), gif)
        scene = ws.write_bytes("output/animation/scene.py",
                               SCENE_TEMPLATE.encode())
        record_artifact(ws.relative(scene),
                        SCENE_TEMPLATE.encode())
        manifest = {"engine": "pillow-gif-1", "title": title,
                    "caption": caption or "", "frames": 6,
                    "size": [WIDTH, HEIGHT]}
        mbytes = json.dumps(manifest, indent=2).encode()
        mtarget = ws.write_bytes("output/animation/scene.json", mbytes)
        record_artifact(ws.relative(mtarget), mbytes)
        html = (f"<!doctype html><meta charset=utf-8><title>{_esc(title)}</title>"
                f"<body><h1>{_esc(title)}</h1><p>{_esc(caption or '')}</p>"
                f'<img src="animation.gif" width="640"/></body>')
        htarget = ws.write_bytes("output/animation/preview.html",
                                 html.encode())
        record_artifact(ws.relative(htarget), html.encode())
        note = "gif+source ready"
        if shutil.which("ffmpeg"):
            try:
                mp4 = ws.resolve("output/animation/animation.mp4")
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", str(target), "-pix_fmt",
                     "yuv420p", str(mp4)], capture_output=True, timeout=60)
                if proc.returncode == 0 and mp4.is_file():
                    data = mp4.read_bytes()
                    record_artifact(ws.relative(mp4), data)
                    note += "; mp4 exported"
                else:
                    print("[runtime] ffmpeg mp4 export failed; GIF stands",
                          file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"[runtime] ffmpeg error (GIF stands): {exc}",
                      file=sys.stderr)
        else:
            note += "; mp4 skipped (ffmpeg not installed)"
        return f"{note}: output/animation/animation.gif ({len(gif)} bytes)"

    return [animation_build]
