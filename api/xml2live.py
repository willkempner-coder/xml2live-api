from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from flask import Flask, Response, jsonify, request

from scripts.generate_ableton_from_premiere_xml import generate_set, read_xml_for_generation
from scripts.parse_premiere_xml import parse_timeline

app = Flask(__name__)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "Template"
DEFAULT_ALLOWED_ORIGINS = (
    "https://wlkonverter.cc",
    "https://www.wlkonverter.cc",
)


def allowed_origins() -> tuple[str, ...]:
    configured = os.getenv("XML2LIVE_ALLOWED_ORIGINS", "")
    if configured.strip():
        return tuple(origin.strip() for origin in configured.split(",") if origin.strip())
    return DEFAULT_ALLOWED_ORIGINS


def cors_origin_for_request() -> str | None:
    origin = request.headers.get("Origin", "").strip()
    return origin if origin and origin in allowed_origins() else None


def add_cors_headers(response: Response) -> Response:
    origin = cors_origin_for_request()
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-XML2LIVE-Token"
        response.headers["Vary"] = "Origin"
    return response


@app.after_request
def apply_cors_headers(response: Response) -> Response:
    return add_cors_headers(response)


def require_allowed_origin() -> Response | None:
    origin = request.headers.get("Origin", "").strip()
    if origin and origin not in allowed_origins():
        return add_cors_headers(jsonify({"error": "Origin not allowed"})), 403
    return None


def require_api_token() -> Response | None:
    expected = os.getenv("XML2LIVE_API_TOKEN", "").strip()
    if not expected:
        return None
    provided = request.headers.get("X-XML2LIVE-Token", "").strip()
    if provided != expected:
        return add_cors_headers(jsonify({"error": "Invalid API token"})), 403
    return None


def template_paths(version: str) -> tuple[Path, Path]:
    if version == "9":
        project = TEMPLATE_ROOT / "XML2LIVE Template Live 9"
        als = project / "Live9Template.als"
        return project, als
    project = TEMPLATE_ROOT / "XML2LIVE Template"
    als = project / "CodexTest.als"
    return project, als


def zip_project(project_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in project_dir.rglob("*"):
            archive.write(item, item.relative_to(project_dir.parent))
    buffer.seek(0)
    return buffer.read()


@app.route("/api/xml2live", methods=["POST", "OPTIONS"])
def xml2live() -> Response:
    origin_error = require_allowed_origin()
    if origin_error is not None:
        return origin_error

    if request.method == "OPTIONS":
        return add_cors_headers(Response(status=204))

    token_error = require_api_token()
    if token_error is not None:
        return token_error

    payload = request.get_json(silent=True) or {}
    xml = payload.get("xml") or {}
    xml_text = xml.get("text")
    if not xml_text:
        return add_cors_headers(jsonify({"error": "Missing xml.text payload"})), 400

    project_name = (payload.get("projectName") or "XML2LIVE Set").strip() or "XML2LIVE Set"
    ableton_version = str(payload.get("abletonVersion") or "11")
    legacy_import_metadata = bool(payload.get("importMetadata"))
    import_markers = bool(payload.get("importSequenceMarkers", legacy_import_metadata))
    import_volume_and_fades = bool(payload.get("importVolumeAndCrossfades", legacy_import_metadata))
    reference = payload.get("referenceMedia") or {}
    reference_name = reference.get("fileName")
    reference_duration = reference.get("durationSeconds")

    temp_root = Path(tempfile.mkdtemp(prefix="xml2live-web-"))
    try:
        xml_path = temp_root / "input.xml"
        xml_path.write_text(xml_text, encoding="utf-8")

        template_project, template_als = template_paths(ableton_version)
        output_project = temp_root / f"{project_name} [XML2LIVE]"

        xml_root = read_xml_for_generation(xml_path)
        timeline = parse_timeline(xml_path)

        als_path = generate_set(
            timeline=timeline,
            xml_root=xml_root,
            template_project=template_project,
            template_als=template_als,
            output_project=output_project,
            bpm=120.0,
            reference_original_media=True,
            import_markers=import_markers,
            import_volume_and_fades=import_volume_and_fades,
            project_name=project_name,
            reference_media_path=reference_name or None,
            reference_media_duration_seconds=float(reference_duration) if reference_duration else None,
        )

        archive_bytes = zip_project(output_project)
        filename = f"{als_path.stem}.zip"
        return Response(
            archive_bytes,
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:  # pragma: no cover - first-pass deploy endpoint
        return add_cors_headers(jsonify({"error": str(exc)})), 500
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
