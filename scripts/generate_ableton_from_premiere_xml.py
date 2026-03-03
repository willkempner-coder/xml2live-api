#!/usr/bin/env python3

import argparse
import copy
import gzip
import math
import shutil
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, Optional
from xml.etree import ElementTree as ET

try:
    from .parse_premiere_xml import AudioTrack, AutomationPoint, Clip, Marker, Timeline, parse_timeline
except ImportError:
    from parse_premiere_xml import AudioTrack, AutomationPoint, Clip, Marker, Timeline, parse_timeline


COLOR_SEQUENCE = ["6", "4", "3", "7", "10", "16", "24"]
GLOBAL_ID_TAGS = {
    "AudioTrack",
    "AutomationTarget",
    "ModulationTarget",
    "Pointee",
    "AudioClip",
    "WarpMarker",
    "VolumeModulationTarget",
    "TranspositionModulationTarget",
    "GrainSizeModulationTarget",
    "FluxModulationTarget",
    "SampleOffsetModulationTarget",
}
LOCAL_ID_TAGS = {"ClipSlot", "TrackSendHolder", "RemoteableTimeSignature", "AutomationLane"}


def beats_from_seconds(seconds: float, bpm: float) -> float:
    return seconds * bpm / 60.0


def format_float(value: float) -> str:
    text = f"{value:.15f}".rstrip("0").rstrip(".")
    return text or "0"


def sanitize_name(name: str) -> str:
    cleaned = "".join(c if c not in '/\\:*?"<>|' else "_" for c in name).strip()
    return cleaned or "Generated Set"


def clamp_gain(value: float) -> float:
    return max(0.0003162277571, min(1.99526238, value))


def semitones_from_speed(playback_speed: float) -> tuple[int, float]:
    if playback_speed <= 0:
        return 0, 0.0
    total_semitones = 12.0 * math.log2(playback_speed)
    coarse = int(round(total_semitones))
    fine = (total_semitones - coarse) * 100.0
    return coarse, fine


def file_crc32(path: Path) -> int:
    crc = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def copy_source_file(
    src: Path,
    imported_dir: Path,
    used_names: Dict[str, int],
    copied_sources: Dict[Path, Path],
) -> Path:
    existing = copied_sources.get(src)
    if existing is not None:
        return existing

    target_name = src.name
    if target_name in used_names:
        used_names[target_name] += 1
        stem = src.stem
        suffix = src.suffix
        target_name = f"{stem} ({used_names[src.name]}){suffix}"
    else:
        used_names[target_name] = 0

    dest = imported_dir / target_name
    shutil.copy2(src, dest)
    copied_sources[src] = dest
    return dest


def find_first(root: ET.Element, tag: str) -> ET.Element:
    elem = next(root.iter(tag), None)
    if elem is None:
        raise ValueError(f"Expected to find <{tag}> in template .als")
    return elem


def child_value(parent: ET.Element, path: str) -> ET.Element:
    elem = parent.find(path)
    if elem is None:
        raise ValueError(f"Missing template path {path}")
    return elem


def optional_child(parent: ET.Element, path: str) -> Optional[ET.Element]:
    return parent.find(path)


def child_value_any(parent: ET.Element, *paths: str) -> ET.Element:
    for path in paths:
        elem = parent.find(path)
        if elem is not None:
            return elem
    raise ValueError(f"Missing template path alternatives: {', '.join(paths)}")


def clear_audio_tracks(tracks_elem: ET.Element) -> None:
    audio_tracks = [t for t in list(tracks_elem) if t.tag == "AudioTrack"]
    for track in audio_tracks:
        tracks_elem.remove(track)


def next_global_id(root: ET.Element) -> int:
    next_id = optional_child(root, "LiveSet/NextPointeeId")
    if next_id is not None:
        return int(next_id.attrib["Value"])

    max_id = 0
    for node in root.iter():
        node_id = node.attrib.get("Id")
        if node_id and node_id.isdigit():
            max_id = max(max_id, int(node_id))
    return max_id + 1


def set_next_global_id(root: ET.Element, next_id: int) -> None:
    node = optional_child(root, "LiveSet/NextPointeeId")
    if node is not None:
        node.set("Value", str(next_id))


def allocate_id(counter: Dict[str, int]) -> int:
    value = counter["next_id"]
    counter["next_id"] += 1
    return value


def retag_ids(elem: ET.Element, counter: Dict[str, int]) -> None:
    for node in elem.iter():
        if "Id" not in node.attrib:
            continue
        if node.tag in LOCAL_ID_TAGS:
            continue
        if node.tag in GLOBAL_ID_TAGS:
            node.set("Id", str(allocate_id(counter)))


def prepare_track(track: ET.Element, track_index: int, clip_count: int) -> None:
    name = child_value(track, "Name/EffectiveName")
    user_name = child_value(track, "Name/UserName")
    memo = optional_child(track, "Name/MemorizedFirstClipName")
    label = f"{track_index}-Audio"
    name.set("Value", label)
    user_name.set("Value", "")
    if memo is not None:
        memo.set("Value", "")
    child_value_any(track, "Color", "ColorIndex").set("Value", COLOR_SEQUENCE[(track_index - 1) % len(COLOR_SEQUENCE)])

    main_seq = find_first(track, "MainSequencer")
    last_selected = child_value(main_seq, "LastSelectedTimeableIndex")
    last_selected.set("Value", "0" if clip_count else "-1")

    arranger_events = child_value(find_first(main_seq, "ArrangerAutomation"), "Events")
    arranger_events.clear()


def apply_track_enabled_state(track: ET.Element, src_track: AudioTrack) -> None:
    mixer = find_first(track, "Mixer")
    child_value(mixer, "On/Manual").set("Value", "true" if src_track.enabled else "false")
    child_value(mixer, "Speaker/Manual").set(
        "Value", "true" if src_track.enabled and not src_track.muted else "false"
    )


def apply_track_volume_metadata(track: ET.Element, src_track: AudioTrack) -> None:
    mixer = find_first(track, "Mixer")
    child_value(mixer, "Volume/Manual").set("Value", format_float(clamp_gain(src_track.volume_level)))


def update_track_name_for_first_clip(track: ET.Element, clip_name: Optional[str]) -> None:
    if not clip_name:
        return
    name = child_value(track, "Name/EffectiveName")
    memo = optional_child(track, "Name/MemorizedFirstClipName")
    name.set("Value", clip_name)
    if memo is not None:
        memo.set("Value", clip_name)


def set_explicit_track_name(track: ET.Element, label: str) -> None:
    child_value(track, "Name/EffectiveName").set("Value", label)
    child_value(track, "Name/UserName").set("Value", label)
    memo = optional_child(track, "Name/MemorizedFirstClipName")
    if memo is not None:
        memo.set("Value", label)


def set_file_ref(file_ref: ET.Element, absolute_path: Path, relative_path: Path, crc32: int, size: int) -> None:
    child_value(file_ref, "RelativePathType").set("Value", "3")
    child_value(file_ref, "RelativePath").set("Value", relative_path.as_posix())
    path_node = optional_child(file_ref, "Path")
    if path_node is not None:
        path_node.set("Value", str(absolute_path))
        child_value(file_ref, "Type").set("Value", "2")
        child_value(file_ref, "LivePackName").set("Value", "")
        child_value(file_ref, "LivePackId").set("Value", "")
        child_value(file_ref, "OriginalFileSize").set("Value", str(size))
        child_value(file_ref, "OriginalCrc").set("Value", str(crc32))
        return

    has_relative = optional_child(file_ref, "HasRelativePath")
    if has_relative is not None:
        has_relative.set("Value", "true")
    child_value(file_ref, "Name").set("Value", absolute_path.name)
    child_value(file_ref, "Type").set("Value", "2")
    for node in list(file_ref.findall("RelativePathElement")):
        file_ref.remove(node)
    relative_parts = relative_path.parts[:-1]
    insert_after = child_value(file_ref, "RelativePath")
    insert_index = list(file_ref).index(insert_after) + 1
    for part in relative_parts:
        file_ref.insert(insert_index, ET.Element("RelativePathElement", {"Dir": part}))
        insert_index += 1
    if (path_hint := optional_child(file_ref, "PathHint")) is not None:
        path_hint.set("Value", str(absolute_path.parent))
    if (search_hint := optional_child(file_ref, "SearchHint")) is not None:
        search_hint.set("Value", absolute_path.name)
    if (file_size := optional_child(file_ref, "FileSize")) is not None:
        file_size.set("Value", str(size))
    if (crc := optional_child(file_ref, "Crc")) is not None:
        crc.set("Value", str(crc32 & 0xFFFFFFFF))
    if (max_crc := optional_child(file_ref, "MaxCrcSize")) is not None and max_crc.attrib.get("Value") == "0":
        max_crc.set("Value", "16384")
    if (extended := optional_child(file_ref, "HasExtendedInfo")) is not None:
        extended.set("Value", "true")
    if (live_pack_name := optional_child(file_ref, "LivePackName")) is not None:
        live_pack_name.set("Value", "")
    if (live_pack_id := optional_child(file_ref, "LivePackId")) is not None:
        live_pack_id.set("Value", "")


def set_file_ref_absolute_only(file_ref: ET.Element, absolute_path: Path, size: int = 0, crc32: int = 0) -> None:
    child_value(file_ref, "RelativePathType").set("Value", "0")
    child_value(file_ref, "RelativePath").set("Value", "")
    path_node = optional_child(file_ref, "Path")
    if path_node is not None:
        path_node.set("Value", str(absolute_path))
        child_value(file_ref, "Type").set("Value", "2")
        child_value(file_ref, "LivePackName").set("Value", "")
        child_value(file_ref, "LivePackId").set("Value", "")
        child_value(file_ref, "OriginalFileSize").set("Value", str(size))
        child_value(file_ref, "OriginalCrc").set("Value", str(crc32))
        return

    has_relative = optional_child(file_ref, "HasRelativePath")
    if has_relative is not None:
        has_relative.set("Value", "false")
    child_value(file_ref, "Name").set("Value", absolute_path.name)
    child_value(file_ref, "Type").set("Value", "2")
    for node in list(file_ref.findall("RelativePathElement")):
        file_ref.remove(node)
    if (path_hint := optional_child(file_ref, "PathHint")) is not None:
        path_hint.set("Value", str(absolute_path.parent))
    if (search_hint := optional_child(file_ref, "SearchHint")) is not None:
        search_hint.set("Value", str(absolute_path))
    if (file_size := optional_child(file_ref, "FileSize")) is not None:
        file_size.set("Value", str(size))
    if (crc := optional_child(file_ref, "Crc")) is not None:
        crc.set("Value", str(crc32 & 0xFFFFFFFF))
    if (max_crc := optional_child(file_ref, "MaxCrcSize")) is not None and max_crc.attrib.get("Value") == "0":
        max_crc.set("Value", "16384")
    if (extended := optional_child(file_ref, "HasExtendedInfo")) is not None:
        extended.set("Value", "true")
    if (live_pack_name := optional_child(file_ref, "LivePackName")) is not None:
        live_pack_name.set("Value", "")
    if (live_pack_id := optional_child(file_ref, "LivePackId")) is not None:
        live_pack_id.set("Value", "")


def apply_clip_enabled_state(clip_elem: ET.Element, clip: Clip) -> None:
    child_value(clip_elem, "Disabled").set("Value", "false" if clip.enabled else "true")


def apply_clip_volume_and_fades(clip_elem: ET.Element, clip: Clip, bpm: float) -> None:
    clip_gain = 1.0 if clip.volume_keyframes else clip.volume_level
    child_value(clip_elem, "SampleVolume").set("Value", format_float(clamp_gain(clip_gain)))

    fades = child_value(clip_elem, "Fades")
    fade_in_beats = beats_from_seconds(clip.fade_in_seconds, bpm)
    fade_out_beats = beats_from_seconds(clip.fade_out_seconds, bpm)
    fade_in_length = 0.0 if clip.crossfade_in else fade_in_beats
    child_value(fades, "FadeInLength").set("Value", format_float(fade_in_length))
    child_value(fades, "FadeOutLength").set("Value", format_float(fade_out_beats))
    child_value(fades, "CrossfadeInState").set("Value", "1" if clip.crossfade_in else "0")
    child_value(fades, "IsDefaultFadeIn").set("Value", "false" if clip.crossfade_in or fade_in_beats > 0 else "true")
    child_value(fades, "IsDefaultFadeOut").set("Value", "false" if fade_out_beats > 0 else "true")


def write_track_volume_automation(track: ET.Element, src_track: AudioTrack, bpm: float) -> None:
    clips_with_keyframes = [clip for clip in src_track.clips if clip.volume_keyframes]
    if not clips_with_keyframes:
        return

    automation_envelopes = optional_child(track, "AutomationEnvelopes/Envelopes")
    if automation_envelopes is None:
        return
    volume_target_id = child_value(find_first(track, "Mixer"), "Volume/AutomationTarget").attrib["Id"]
    envelope = ET.SubElement(automation_envelopes, "AutomationEnvelope", {"Id": str(len(list(automation_envelopes)))})
    envelope_target = ET.SubElement(envelope, "EnvelopeTarget")
    ET.SubElement(envelope_target, "PointeeId", {"Value": volume_target_id})
    automation = ET.SubElement(envelope, "Automation")
    events = ET.SubElement(automation, "Events")

    base_level = clamp_gain(src_track.volume_level)
    points: list[tuple[float, float]] = [(-63072000.0, base_level)]

    for clip in clips_with_keyframes:
        clip_start = beats_from_seconds(clip.start_seconds, bpm)
        clip_end = beats_from_seconds(clip.end_seconds, bpm)
        ordered = sorted(clip.volume_keyframes, key=lambda point: point.time_seconds)
        if not ordered:
            continue

        points.append((clip_start, base_level))

        for point in ordered:
            points.append((beats_from_seconds(clip.start_seconds + point.time_seconds, bpm), clamp_gain(base_level * point.value)))

        last_level = clamp_gain(base_level * ordered[-1].value)
        points.append((clip_end, last_level))
        points.append((clip_end, base_level))

    points.sort(key=lambda item: item[0])

    for index, (time, value) in enumerate(points):
        ET.SubElement(
            events,
            "FloatEvent",
            {
                "Id": str(index),
                "Time": format_float(time),
                "Value": format_float(value),
            },
        )

    transform = ET.SubElement(automation, "AutomationTransformViewState")
    ET.SubElement(transform, "IsTransformPending", {"Value": "false"})
    ET.SubElement(transform, "TimeAndValueTransforms")


def write_locators(root: ET.Element, markers: Iterable[Marker], bpm: float) -> None:
    locators_outer = child_value(root, "LiveSet/Locators")
    locators = child_value(locators_outer, "Locators")
    locators.clear()

    for index, marker in enumerate(markers):
        locator = ET.SubElement(locators, "Locator", {"Id": str(index)})
        ET.SubElement(locator, "LomId", {"Value": "0"})
        ET.SubElement(locator, "Time", {"Value": format_float(beats_from_seconds(marker.time_seconds, bpm))})
        ET.SubElement(locator, "Name", {"Value": marker.name})
        ET.SubElement(locator, "Annotation", {"Value": ""})
        ET.SubElement(locator, "IsSongStart", {"Value": "false"})


def build_clip(
    template_clip: ET.Element,
    clip: Clip,
    source_file: Path,
    project_dir: Path,
    sample_rate: int,
    source_duration_seconds: float,
    bpm: float,
    counter: Dict[str, int],
    color: str,
    reference_original_media: bool,
    import_volume_and_fades: bool,
) -> ET.Element:
    clip_elem = copy.deepcopy(template_clip)
    retag_ids(clip_elem, counter)

    start_beats = beats_from_seconds(clip.start_seconds, bpm)
    end_beats = beats_from_seconds(clip.end_seconds, bpm)
    clip_len_beats = beats_from_seconds(clip.end_seconds - clip.start_seconds, bpm)

    clip_elem.set("Time", format_float(start_beats))
    child_value(clip_elem, "CurrentStart").set("Value", format_float(start_beats))
    child_value(clip_elem, "CurrentEnd").set("Value", format_float(end_beats))
    child_value(clip_elem, "Name").set("Value", clip.name)
    child_value_any(clip_elem, "Color", "ColorIndex").set("Value", color)
    apply_clip_enabled_state(clip_elem, clip)
    child_value(clip_elem, "IsWarped").set("Value", "false")

    coarse_pitch, fine_pitch = semitones_from_speed(clip.playback_speed)
    child_value(clip_elem, "PitchCoarse").set("Value", str(coarse_pitch))
    child_value(clip_elem, "PitchFine").set("Value", format_float(fine_pitch))

    loop = child_value(clip_elem, "Loop")
    # Ableton's crossfades behave closest to Premiere when the visible incoming
    # source starts later, but the full underlying handle stays in HiddenLoopStart.
    loop_start_seconds = clip.visible_in_seconds if clip.fade_in_seconds > 0 else clip.in_seconds
    hidden_loop_end_seconds = clip.visible_out_seconds if clip.fade_out_seconds > 0 else clip.out_seconds
    child_value(loop, "LoopStart").set("Value", format_float(loop_start_seconds))
    # Outgoing faded clips keep the real source out-point in LoopEnd while the
    # earlier audible trim lives in HiddenLoopEnd.
    child_value(loop, "LoopEnd").set("Value", format_float(clip.out_seconds))
    child_value(loop, "StartRelative").set("Value", "0")
    child_value(loop, "LoopOn").set("Value", "false")
    child_value(loop, "OutMarker").set("Value", format_float(clip.out_seconds))
    child_value(loop, "HiddenLoopStart").set("Value", format_float(clip.in_seconds))
    child_value(loop, "HiddenLoopEnd").set("Value", format_float(hidden_loop_end_seconds))

    sample_ref = child_value(clip_elem, "SampleRef")
    file_ref = child_value(sample_ref, "FileRef")
    if reference_original_media:
        size = source_file.stat().st_size if source_file.exists() else 0
        crc32 = file_crc32(source_file) if source_file.exists() else 0
        set_file_ref_absolute_only(file_ref, source_file, size=size, crc32=crc32)
        last_mod = int(source_file.stat().st_mtime) if source_file.exists() else 0
    else:
        size = source_file.stat().st_size
        crc32 = file_crc32(source_file)
        relative = source_file.relative_to(project_dir)
        set_file_ref(file_ref, source_file, relative, crc32, size)
        last_mod = int(source_file.stat().st_mtime)
    child_value(sample_ref, "LastModDate").set("Value", str(last_mod))
    child_value(sample_ref, "DefaultDuration").set("Value", str(max(1, round(source_duration_seconds * sample_rate))))
    child_value(sample_ref, "DefaultSampleRate").set("Value", str(sample_rate))

    warp_markers = child_value(clip_elem, "WarpMarkers")
    markers = list(warp_markers)
    if len(markers) < 2:
        raise ValueError("Template clip is missing warp markers")
    markers[0].set("SecTime", format_float(clip.in_seconds))
    markers[0].set("BeatTime", "0")
    markers[1].set("SecTime", format_float(clip.in_seconds + 0.015625))
    markers[1].set("BeatTime", "0.03125")
    for extra in markers[2:]:
        warp_markers.remove(extra)

    time_sigs = child_value(clip_elem, "TimeSignature/TimeSignatures")
    for extra in list(time_sigs)[1:]:
        time_sigs.remove(extra)

    if import_volume_and_fades:
        apply_clip_volume_and_fades(clip_elem, clip, bpm)

    return clip_elem


def collect_file_metadata(sequence: ET.Element) -> Dict[str, Dict[str, float]]:
    files: Dict[str, Dict[str, float]] = {}
    for file_elem in sequence.findall(".//file[@id]"):
        file_id = file_elem.attrib["id"]
        samplerate_text = file_elem.findtext("media/audio/samplecharacteristics/samplerate")
        duration_text = file_elem.findtext("duration")
        rate_elem = file_elem.find("rate")
        fps = None
        if rate_elem is not None:
            timebase = rate_elem.findtext("timebase")
            ntsc = (rate_elem.findtext("ntsc") or "").strip().upper() == "TRUE"
            if timebase:
                base = float(timebase)
                fps = base * 1000.0 / 1001.0 if ntsc else base
        duration_seconds = None
        if duration_text and fps:
            duration_seconds = int(duration_text) / fps
        files[file_id] = {
            "sample_rate": int(float(samplerate_text)) if samplerate_text else 44100,
            "duration_seconds": duration_seconds or 0.0,
        }
    return files


def prepare_project_copy(template_project: Path, output_project: Path) -> Path:
    if output_project.exists():
        shutil.rmtree(output_project)
    shutil.copytree(template_project, output_project)

    for als_file in output_project.glob("*.als"):
        als_file.unlink()

    backup_dir = output_project / "Backup"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    imported_dir = output_project / "Samples" / "Imported"
    imported_dir.mkdir(parents=True, exist_ok=True)
    for item in imported_dir.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
    return imported_dir


def append_before_returns(tracks_elem: ET.Element, track_elem: ET.Element) -> None:
    children = list(tracks_elem)
    for index, child in enumerate(children):
        if child.tag == "ReturnTrack":
            tracks_elem.insert(index, track_elem)
            return
    tracks_elem.append(track_elem)


def is_video_path(path: Path) -> bool:
    return path.suffix.lower() in {".mov", ".mp4", ".m4v", ".mpg", ".mpeg", ".avi", ".mkv"}


def build_reference_track(reference_media_path: str, duration_seconds: float) -> AudioTrack:
    media_path = Path(reference_media_path)
    clip_name = media_path.stem or "REF"
    safe_duration = max(0.1, duration_seconds)
    reference_clip = Clip(
        clip_id="reference-video",
        name=clip_name,
        source_track=None,
        file_id=None,
        path=str(media_path),
        start_frames=0,
        end_frames=1,
        in_frames=0,
        out_frames=1,
        duration_frames=1,
        start_seconds=0.0,
        end_seconds=safe_duration,
        in_seconds=0.0,
        out_seconds=safe_duration,
        visible_in_seconds=0.0,
        visible_out_seconds=safe_duration,
        enabled=True,
        volume_level=1.0,
        fade_in_seconds=0.0,
        fade_out_seconds=0.0,
        crossfade_in=False,
        volume_keyframes=[],
        playback_speed=1.0,
        reverse=False,
    )
    return AudioTrack(
        index=0,
        clip_count=1,
        enabled=True,
        muted=True,
        volume_level=1.0,
        clips=[reference_clip],
    )


def update_transport(root: ET.Element) -> None:
    child_value(root, "LiveSet/Transport/CurrentTime").set("Value", "0")
    child_value(root, "LiveSet/Transport/LoopOn").set("Value", "false")


def update_master_tempo(root: ET.Element, bpm: float) -> None:
    master_track = child_value(root, "LiveSet/MasterTrack")
    tempo = child_value(find_first(master_track, "Tempo"), "Manual")
    tempo.set("Value", format_float(bpm))


def generate_set(
    timeline: Timeline,
    xml_root: ET.Element,
    template_project: Path,
    template_als: Path,
    output_project: Path,
    bpm: float,
    reference_original_media: bool = False,
    import_markers: bool = False,
    import_volume_and_fades: bool = False,
    project_name: Optional[str] = None,
    reference_media_path: Optional[str] = None,
    reference_media_duration_seconds: Optional[float] = None,
) -> Path:
    imported_dir = prepare_project_copy(template_project, output_project)

    with gzip.open(template_als, "rb") as handle:
        root = ET.fromstring(handle.read())

    update_transport(root)
    update_master_tempo(root, bpm)
    if import_markers:
        write_locators(root, timeline.markers, bpm)

    tracks_elem = child_value(root, "LiveSet/Tracks")
    template_audio_tracks = [t for t in list(tracks_elem) if t.tag == "AudioTrack"]
    if not template_audio_tracks:
        raise ValueError("Template set has no audio tracks")

    filled_track_template = copy.deepcopy(template_audio_tracks[0])
    empty_track_template = copy.deepcopy(template_audio_tracks[-1])
    clip_template = next(filled_track_template.iter("AudioClip"), None)
    if clip_template is None:
        raise ValueError("Template set has no audio clip template to clone")

    clear_audio_tracks(tracks_elem)

    sequence = child_value(xml_root, "sequence")
    file_meta = collect_file_metadata(sequence)

    id_counter = {"next_id": next_global_id(root)}
    used_names: Dict[str, int] = {}
    copied_sources: Dict[Path, Path] = {}

    audio_tracks: list[AudioTrack] = list(timeline.audio_tracks)
    if reference_media_path:
        reference_track = build_reference_track(
            reference_media_path=reference_media_path,
            duration_seconds=reference_media_duration_seconds or timeline.duration_seconds or 1.0,
        )
        audio_tracks = [reference_track] + audio_tracks

    audio_tracks_to_write: Iterable[AudioTrack] = [
        replace(track, index=track_index, clip_count=len(track.clips))
        for track_index, track in enumerate(audio_tracks, start=1)
    ]
    for track_index, src_track in enumerate(audio_tracks_to_write, start=1):
        track_template = filled_track_template if src_track.clip_count else empty_track_template
        track_elem = copy.deepcopy(track_template)
        retag_ids(track_elem, id_counter)
        prepare_track(track_elem, track_index, src_track.clip_count)
        apply_track_enabled_state(track_elem, src_track)
        if import_volume_and_fades:
            apply_track_volume_metadata(track_elem, src_track)
            write_track_volume_automation(track_elem, src_track, bpm)

        color = child_value_any(track_elem, "Color", "ColorIndex").attrib["Value"]
        events = child_value(child_value(find_first(find_first(track_elem, "MainSequencer"), "ArrangerAutomation"), "Events"), ".")

        for clip in src_track.clips:
            if not clip.path:
                continue
            source_path = Path(clip.path)
            if not reference_original_media and not source_path.exists():
                continue
            target_file = source_path
            if not reference_original_media and source_path.exists():
                target_file = copy_source_file(source_path, imported_dir, used_names, copied_sources)
            meta = file_meta.get(clip.file_id or "", {})
            sample_rate = int(meta.get("sample_rate", 48000 if is_video_path(source_path) else 44100))
            duration_seconds = float(meta.get("duration_seconds", 0.0))
            if duration_seconds <= 0:
                duration_seconds = clip.out_seconds
            new_clip = build_clip(
                template_clip=clip_template,
                clip=clip,
                source_file=target_file,
                project_dir=output_project,
                sample_rate=sample_rate,
                source_duration_seconds=duration_seconds,
                bpm=bpm,
                counter=id_counter,
                color=color,
                reference_original_media=reference_original_media,
                import_volume_and_fades=import_volume_and_fades,
            )
            events.append(new_clip)

        if reference_media_path and track_index == 1:
            set_explicit_track_name(track_elem, "REF")
        elif src_track.clips:
            update_track_name_for_first_clip(track_elem, src_track.clips[0].name)

        append_before_returns(tracks_elem, track_elem)

    set_next_global_id(root, id_counter["next_id"])

    set_name = sanitize_name(project_name or timeline.sequence_name)
    als_path = output_project / f"{set_name}.als"
    if als_path.exists():
        als_path.unlink()
    with gzip.open(als_path, "wb") as handle:
        handle.write(ET.tostring(root, encoding="utf-8", xml_declaration=True))

    return als_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an Ableton Live Set from a Premiere XML export using a template project."
    )
    parser.add_argument("xml_path", type=Path, help="Path to a Premiere XML file")
    parser.add_argument("template_project", type=Path, help="Path to an Ableton project folder to use as a template")
    parser.add_argument(
        "--template-als",
        type=Path,
        help="Path to the template .als inside the project folder. Defaults to the first .als found at the project root.",
    )
    parser.add_argument(
        "--output-project",
        type=Path,
        required=True,
        help="Directory for the generated Ableton project",
    )
    parser.add_argument(
        "--bpm",
        type=float,
        default=120.0,
        help="Tempo to use when converting Premiere seconds into Ableton song time",
    )
    parser.add_argument(
        "--reference-original-media",
        action="store_true",
        help="Leave the Ableton set pointing at the original source paths instead of copying media into the project",
    )
    parser.add_argument(
        "--import-markers",
        action="store_true",
        help="Import sequence markers when available",
    )
    parser.add_argument(
        "--import-volume-and-fades",
        action="store_true",
        help="Import track and clip volume, volume automation, fades, and crossfades when available",
    )
    parser.add_argument(
        "--project-name",
        type=str,
        help="Custom name for the generated Ableton set",
    )
    parser.add_argument(
        "--reference-media-path",
        type=str,
        help="Optional path to a reference video or audio file to place on a muted REF track at the start of the set",
    )
    parser.add_argument(
        "--reference-media-duration-seconds",
        type=float,
        help="Duration of the optional reference media in seconds",
    )
    args = parser.parse_args()

    xml_root = read_xml_for_generation(args.xml_path)
    timeline = parse_timeline(args.xml_path)

    template_als = args.template_als
    if template_als is None:
        try:
            template_als = next(p for p in args.template_project.iterdir() if p.suffix == ".als")
        except StopIteration as exc:
            raise ValueError("Could not find a template .als in the template project folder") from exc

    als_path = generate_set(
        timeline=timeline,
        xml_root=xml_root,
        template_project=args.template_project,
        template_als=template_als,
        output_project=args.output_project,
        bpm=args.bpm,
        reference_original_media=args.reference_original_media,
        import_markers=args.import_markers,
        import_volume_and_fades=args.import_volume_and_fades,
        project_name=args.project_name,
        reference_media_path=args.reference_media_path,
        reference_media_duration_seconds=args.reference_media_duration_seconds,
    )
    print(als_path)


def read_xml_for_generation(path: Path) -> ET.Element:
    text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    return ET.fromstring(text)


if __name__ == "__main__":
    main()
