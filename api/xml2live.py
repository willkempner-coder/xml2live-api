from __future__ import annotations

import io
import json
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


@app.route("/api/xml2live", methods=["POST"])
def xml2live() -> Response:
    payload = request.get_json(silent=True) or {}
    xml = payload.get("xml") or {}
    xml_text = xml.get("text")
    if not xml_text:
        return jsonify({"error": "Missing xml.text payload"}), 400

    project_name = (payload.get("projectName") or "XML2LIVE Set").strip() or "XML2LIVE Set"
    ableton_version = str(payload.get("abletonVersion") or "11")
    import_metadata = bool(payload.get("importMetadata"))
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
            import_mix_metadata=import_metadata,
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
        return jsonify({"error": str(exc)}), 500
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
