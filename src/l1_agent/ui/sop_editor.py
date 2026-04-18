"""SOP Authoring UI — aiohttp web routes for creating and editing SOPs.

Mounts at /sop-editor/ and provides:
  GET  /sop-editor/             — list all SOPs
  GET  /sop-editor/new          — blank SOP form
  GET  /sop-editor/{sop_id}     — edit existing SOP
  POST /sop-editor/save         — save (create or update) SOP JSON file
  POST /sop-editor/{sop_id}/delete — delete SOP file

SOPs are stored as JSON files under the configured sop_dir (default: data/sample_sops/).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional

from aiohttp import web

from src.l1_agent.utils.logging import get_logger

logger = get_logger("sop_editor")

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

_HTML_HEADER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>L1 Agent — SOP Editor</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          margin: 0; background: #f5f7fa; color: #1a1a2e; }}
  .topbar {{ background: #1a1a2e; color: #fff; padding: 12px 24px;
             display: flex; align-items: center; gap: 16px; }}
  .topbar h1 {{ margin: 0; font-size: 1.1rem; }}
  .topbar a {{ color: #93c5fd; text-decoration: none; font-size: 0.9rem; }}
  .container {{ max-width: 960px; margin: 32px auto; padding: 0 16px; }}
  .card {{ background: #fff; border-radius: 8px; padding: 24px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 24px; }}
  h2 {{ margin-top: 0; font-size: 1.2rem; color: #1a1a2e; }}
  label {{ display: block; margin-bottom: 4px; font-size: 0.85rem;
           font-weight: 600; color: #374151; }}
  input[type=text], textarea, select {{
    width: 100%; padding: 8px 10px; border: 1px solid #d1d5db;
    border-radius: 6px; font-size: 0.9rem; box-sizing: border-box;
    margin-bottom: 16px; }}
  textarea {{ min-height: 320px; font-family: monospace; font-size: 0.85rem; }}
  .btn {{ display: inline-block; padding: 9px 20px; border-radius: 6px;
          border: none; cursor: pointer; font-size: 0.9rem; font-weight: 600; }}
  .btn-primary {{ background: #3b82f6; color: #fff; }}
  .btn-primary:hover {{ background: #2563eb; }}
  .btn-danger {{ background: #ef4444; color: #fff; }}
  .btn-danger:hover {{ background: #dc2626; }}
  .btn-secondary {{ background: #e5e7eb; color: #374151; text-decoration: none; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #e5e7eb; }}
  th {{ font-size: 0.8rem; color: #6b7280; text-transform: uppercase; }}
  tr:hover td {{ background: #f9fafb; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
            font-size: 0.75rem; font-weight: 600; }}
  .badge-blue {{ background: #dbeafe; color: #1d4ed8; }}
  .flash {{ padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; }}
  .flash-ok {{ background: #d1fae5; color: #065f46; }}
  .flash-err {{ background: #fee2e2; color: #991b1b; }}
</style>
</head>
<body>
<div class="topbar">
  <h1>L1 Agent</h1>
  <a href="/sop-editor/">SOP Library</a>
  <a href="/sop-editor/new">+ New SOP</a>
  <a href="/health">Health</a>
  <a href="/metrics">Metrics</a>
</div>
<div class="container">
"""

_HTML_FOOTER = "</div></body></html>"


class SOPEditorRoutes:
    """Registers the SOP editor aiohttp routes onto an app."""

    def __init__(self, sop_dir: str = "data/sample_sops") -> None:
        self._sop_dir = Path(sop_dir)
        self._sop_dir.mkdir(parents=True, exist_ok=True)

    def register(self, app: web.Application) -> None:
        app.router.add_get("/sop-editor/", self._list_sops)
        app.router.add_get("/sop-editor/new", self._new_form)
        app.router.add_get("/sop-editor/{sop_id}", self._edit_form)
        app.router.add_post("/sop-editor/save", self._save_sop)
        app.router.add_post("/sop-editor/{sop_id}/delete", self._delete_sop)

    # ── Helpers ───────────────────────────────────────────────────────

    def _sop_files(self) -> List[Path]:
        return sorted(self._sop_dir.glob("*.json"))

    def _load_sop(self, sop_id: str) -> Optional[dict]:
        path = self._sop_dir / f"{sop_id}.json"
        if not path.exists():
            # Try matching by sop_id field inside any file
            for f in self._sop_files():
                try:
                    data = json.loads(f.read_text())
                    if data.get("sop_id") == sop_id:
                        return data
                except Exception:
                    pass
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    # ── List ──────────────────────────────────────────────────────────

    async def _list_sops(self, request: web.Request) -> web.Response:
        rows = []
        for f in self._sop_files():
            try:
                data = json.loads(f.read_text())
                sop_id = data.get("sop_id", f.stem)
                title = data.get("title", "(untitled)")
                version = data.get("version", "–")
                services = ", ".join(data.get("applicable_services", []))
                rows.append(
                    f"<tr>"
                    f'<td><a href="/sop-editor/{sop_id}">{sop_id}</a></td>'
                    f"<td>{title}</td>"
                    f'<td><span class="badge badge-blue">v{version}</span></td>'
                    f"<td>{services}</td>"
                    f'<td><a class="btn btn-secondary" style="padding:4px 10px;font-size:.8rem" '
                    f'href="/sop-editor/{sop_id}">Edit</a></td>'
                    f"</tr>"
                )
            except Exception:
                pass

        table = (
            "<table><thead><tr>"
            "<th>SOP ID</th><th>Title</th><th>Version</th>"
            "<th>Services</th><th></th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        ) if rows else "<p>No SOPs found. <a href='/sop-editor/new'>Create the first one</a>.</p>"

        html = (
            _HTML_HEADER
            + '<div class="card"><h2>SOP Library</h2>'
            + f'<a class="btn btn-primary" href="/sop-editor/new" style="margin-bottom:16px">+ New SOP</a>'
            + table
            + "</div>"
            + _HTML_FOOTER
        )
        return web.Response(text=html, content_type="text/html")

    # ── New / Edit form ───────────────────────────────────────────────

    async def _new_form(self, request: web.Request) -> web.Response:
        template = {
            "sop_id": "SOP-NEW-001",
            "title": "New SOP Title",
            "applicable_services": [],
            "applicable_categories": [],
            "applicable_assignment_groups": [],
            "keywords": [],
            "steps": [],
            "pre_checks": [],
            "tools_required": [],
            "escalation_criteria": {
                "max_retry_count": 3,
                "escalate_on_access_denied": True,
                "escalate_on_ambiguous_result": True,
                "escalate_on_write_action": True,
            },
            "version": "1.0",
        }
        return self._render_form(template, flash=None)

    async def _edit_form(self, request: web.Request) -> web.Response:
        sop_id = request.match_info["sop_id"]
        if not _SAFE_ID_RE.match(sop_id):
            raise web.HTTPBadRequest(text="Invalid SOP ID")

        data = self._load_sop(sop_id)
        if data is None:
            raise web.HTTPNotFound(text=f"SOP '{sop_id}' not found")

        flash = request.rel_url.query.get("saved")
        flash_html = (
            f'<div class="flash flash-ok">SOP saved successfully.</div>' if flash else ""
        )
        return self._render_form(data, flash=flash_html)

    def _render_form(self, sop: dict, flash: Optional[str]) -> web.Response:
        json_str = json.dumps(sop, indent=2)
        sop_id = sop.get("sop_id", "")
        title = sop.get("title", "")
        delete_btn = (
            f'<form method="post" action="/sop-editor/{sop_id}/delete" '
            f'onsubmit="return confirm(\'Delete {sop_id}?\')" style="display:inline">'
            f'<button class="btn btn-danger" type="submit">Delete</button></form>'
            if sop_id
            else ""
        )
        html = (
            _HTML_HEADER
            + (flash or "")
            + '<div class="card">'
            + f"<h2>{'Edit' if sop_id else 'New'} SOP: {title}</h2>"
            + '<form method="post" action="/sop-editor/save">'
            + '<label>SOP JSON (edit directly or use the fields below)</label>'
            + f'<textarea name="sop_json" spellcheck="false">{json_str}</textarea>'
            + '<div style="display:flex;gap:12px;align-items:center">'
            + '<button class="btn btn-primary" type="submit">Save SOP</button>'
            + f'<a class="btn btn-secondary" href="/sop-editor/">Cancel</a>'
            + delete_btn
            + "</div>"
            + "</form>"
            + "</div>"
            + _HTML_FOOTER
        )
        return web.Response(text=html, content_type="text/html")

    # ── Save ──────────────────────────────────────────────────────────

    async def _save_sop(self, request: web.Request) -> web.Response:
        post = await request.post()
        raw = post.get("sop_json", "").strip()
        if not raw:
            raise web.HTTPBadRequest(text="sop_json field is required")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            html = (
                _HTML_HEADER
                + f'<div class="flash flash-err">Invalid JSON: {exc}</div>'
                + '<a class="btn btn-secondary" href="javascript:history.back()">Go back</a>'
                + _HTML_FOOTER
            )
            return web.Response(text=html, content_type="text/html", status=400)

        sop_id = data.get("sop_id", "").strip()
        if not sop_id or not _SAFE_ID_RE.match(sop_id):
            html = (
                _HTML_HEADER
                + '<div class="flash flash-err">sop_id is required and must be alphanumeric/dashes only.</div>'
                + '<a class="btn btn-secondary" href="javascript:history.back()">Go back</a>'
                + _HTML_FOOTER
            )
            return web.Response(text=html, content_type="text/html", status=400)

        path = self._sop_dir / f"{sop_id}.json"
        path.write_text(json.dumps(data, indent=2))
        logger.info("SOP saved: %s → %s", sop_id, path)

        raise web.HTTPFound(location=f"/sop-editor/{sop_id}?saved=1")

    # ── Delete ────────────────────────────────────────────────────────

    async def _delete_sop(self, request: web.Request) -> web.Response:
        sop_id = request.match_info["sop_id"]
        if not _SAFE_ID_RE.match(sop_id):
            raise web.HTTPBadRequest(text="Invalid SOP ID")

        path = self._sop_dir / f"{sop_id}.json"
        if path.exists():
            path.unlink()
            logger.info("SOP deleted: %s", sop_id)

        raise web.HTTPFound(location="/sop-editor/")
