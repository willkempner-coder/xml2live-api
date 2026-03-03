#!/usr/bin/env python3

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET


@dataclass
class AutomationPoint:
    time_seconds: float
    value: float


@dataclass
class Clip:
    clip_id: str
    name: str
    source_track: Optional[str]
    file_id: Optional[str]
    path: Optional[str]
    start_frames: int
    end_frames: int
    in_frames: int
    out_frames: int
    duration_frames: int
    start_seconds: float
    end_seconds: float
    in_seconds: float
    out_seconds: float
    visible_in_seconds: float
    visible_out_seconds: float
    enabled: bool
    volume_level: float
    fade_in_seconds: float
    fade_out_seconds: float
    crossfade_in: bool
    volume_keyframes: List[AutomationPoint]


@dataclass
class Marker:
    name: str
    time_seconds: float


@dataclass
class AudioTrack:
    index: int
    clip_count: int
    enabled: bool
    muted: bool
    volume_level: float
    clips: List[Clip]


@dataclass
class Timeline:
    sequence_name: str
    frame_rate: float
    duration_frames: int
    duration_seconds: float
    markers: List[Marker]
    audio_tracks: List[AudioTrack]


def read_xml(path: Path) -> ET.Element:
    text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    return ET.fromstring(text)


def parse_pathurl(pathurl: Optional[str]) -> Optional[str]:
    if not pathurl:
        return None
    parsed = urlparse(pathurl)
    if parsed.scheme != "file":
        return pathurl
    return unquote(parsed.path)


def file_lookup(root: ET.Element) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for file_elem in root.findall(".//file[@id]"):
        path = parse_pathurl(file_elem.findtext("pathurl"))
        if path:
            lookup[file_elem.attrib["id"]] = path
    return lookup


def frame_rate_of(node: ET.Element) -> float:
    timebase = node.findtext("rate/timebase")
    ntsc = (node.findtext("rate/ntsc") or "").strip().upper() == "TRUE"
    if not timebase:
        raise ValueError("Missing rate/timebase in Premiere XML")
    base = float(timebase)
    return base * 1000.0 / 1001.0 if ntsc else base


def infer_effective_frame_rate(sequence: ET.Element, fallback_fps: float) -> float:
    ticks_per_second = 254_016_000_000
    inferred: List[float] = []
    for clip_elem in sequence.findall(".//media/audio/track/clipitem"):
        try:
            in_frames = int(clip_elem.findtext("in") or "0")
            out_frames = int(clip_elem.findtext("out") or "0")
            ppro_in = int(clip_elem.findtext("pproTicksIn") or "0")
            ppro_out = int(clip_elem.findtext("pproTicksOut") or "0")
        except ValueError:
            continue

        frame_delta = out_frames - in_frames
        tick_delta = ppro_out - ppro_in
        if frame_delta <= 0 or tick_delta <= 0:
            continue

        seconds = tick_delta / ticks_per_second
        if seconds <= 0:
            continue
        inferred.append(frame_delta / seconds)

    if not inferred:
        return fallback_fps
    return median(inferred)


def parse_int(node: ET.Element, tag: str) -> int:
    value = node.findtext(tag)
    if value is None:
        raise ValueError(f"Missing <{tag}> in clip {node.attrib.get('id', '')}")
    return int(value)


def parse_bool_text(value: Optional[str], default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().upper() not in {"FALSE", "0", "NO"}


def extract_gain_data(node: ET.Element, clip_in_frames: Optional[int] = None, fps: float = 0.0) -> tuple[float, List[AutomationPoint]]:
    default_value = 1.0
    keyframes: List[AutomationPoint] = []
    for effect in node.findall("./filter/effect"):
        effect_name = " ".join(
            filter(
                None,
                [
                    effect.findtext("name"),
                    effect.findtext("effectid"),
                    effect.findtext("effectcategory"),
                ],
            )
        ).lower()
        for parameter in effect.findall("parameter"):
            names = " ".join(
                filter(
                    None,
                    [
                        parameter.findtext("name"),
                        parameter.findtext("parameterid"),
                    ],
                )
            ).lower()
            effect_and_names = f"{effect_name} {names}"
            if not any(keyword in effect_and_names for keyword in ("level", "volume")):
                continue
            value_text = parameter.findtext("value")
            try:
                if value_text is not None:
                    default_value = float(value_text)
            except ValueError:
                pass
            if clip_in_frames is not None and fps > 0:
                for keyframe in parameter.findall("keyframe"):
                    when_text = keyframe.findtext("when")
                    key_value_text = keyframe.findtext("value")
                    if when_text is None or key_value_text is None:
                        continue
                    try:
                        when_frames = int(when_text)
                        point_value = float(key_value_text)
                    except ValueError:
                        continue
                    keyframes.append(
                        AutomationPoint(
                            time_seconds=max(0.0, (when_frames - clip_in_frames) / fps),
                            value=point_value,
                        )
                    )
            return default_value, keyframes
    return default_value, keyframes


def parse_markers(sequence: ET.Element, fps: float) -> List[Marker]:
    markers: List[Marker] = []
    for marker_elem in sequence.findall("marker"):
        name = (
            marker_elem.findtext("name")
            or marker_elem.findtext("comment")
            or marker_elem.findtext("in")
            or "Marker"
        )
        time_text = marker_elem.findtext("in") or marker_elem.findtext("start")
        if time_text is None:
            continue
        try:
            time_frames = int(time_text)
        except ValueError:
            continue
        markers.append(Marker(name=name, time_seconds=time_frames / fps if fps else 0.0))
    return markers


def transition_cut_point_frames(transition_elem: ET.Element, fps: float) -> int:
    cut_point_ticks = transition_elem.findtext("cutPointTicks")
    if cut_point_ticks and fps > 0:
        ticks_per_second = 254_016_000_000
        ticks_per_frame = ticks_per_second / fps
        try:
            return max(0, round(int(cut_point_ticks) / ticks_per_frame))
        except ValueError:
            pass

    try:
        start_frames = int(transition_elem.findtext("start") or "0")
        end_frames = int(transition_elem.findtext("end") or "0")
    except ValueError:
        return 0
    return max(0, round((end_frames - start_frames) / 2))


def infer_start_from_transition(transition_elem: Optional[ET.Element], fps: float) -> Optional[int]:
    if transition_elem is None:
        return None
    try:
        start_frames = int(transition_elem.findtext("start") or "0")
        end_frames = int(transition_elem.findtext("end") or "0")
    except ValueError:
        return None

    alignment = (transition_elem.findtext("alignment") or "").strip().lower()
    if alignment == "center":
        return start_frames + transition_cut_point_frames(transition_elem, fps)
    if alignment == "start-black":
        return start_frames
    if alignment == "end-black":
        return end_frames
    return start_frames


def infer_end_from_transition(transition_elem: Optional[ET.Element], fps: float) -> Optional[int]:
    if transition_elem is None:
        return None
    try:
        start_frames = int(transition_elem.findtext("start") or "0")
        end_frames = int(transition_elem.findtext("end") or "0")
    except ValueError:
        return None

    alignment = (transition_elem.findtext("alignment") or "").strip().lower()
    if alignment == "center":
        return start_frames + transition_cut_point_frames(transition_elem, fps)
    if alignment == "start-black":
        return start_frames
    if alignment == "end-black":
        return end_frames
    return end_frames


def transition_duration_seconds(transition_elem: Optional[ET.Element], fps: float) -> float:
    if transition_elem is None or fps <= 0:
        return 0.0
    try:
        start_frames = int(transition_elem.findtext("start") or "0")
        end_frames = int(transition_elem.findtext("end") or "0")
    except ValueError:
        return 0.0
    return max(0.0, (end_frames - start_frames) / fps)


def clip_from_xml(
    clip_elem: ET.Element,
    fps: float,
    files: Dict[str, str],
    sequence_duration_frames: int,
    previous_transition: Optional[ET.Element] = None,
    next_transition: Optional[ET.Element] = None,
) -> Clip:
    file_elem = clip_elem.find("file")
    file_id = file_elem.attrib.get("id") if file_elem is not None else None
    path = None
    if file_elem is not None:
        path = parse_pathurl(file_elem.findtext("pathurl"))
    if not path and file_id:
        path = files.get(file_id)

    start_frames = parse_int(clip_elem, "start")
    end_frames = parse_int(clip_elem, "end")
    in_frames = parse_int(clip_elem, "in")
    out_frames = parse_int(clip_elem, "out")
    ppro_ticks_in = clip_elem.findtext("pproTicksIn")
    ppro_ticks_out = clip_elem.findtext("pproTicksOut")

    source_track = clip_elem.findtext("sourcetrack/trackindex")
    if start_frames < 0:
        inferred_start = infer_start_from_transition(previous_transition, fps)
        start_frames = inferred_start if inferred_start is not None else 0
    if end_frames < 0:
        inferred_end = infer_end_from_transition(next_transition, fps)
        if inferred_end is not None:
            end_frames = inferred_end
        else:
            inferred_duration_frames = max(0, out_frames - in_frames)
            if inferred_duration_frames > 0:
                end_frames = start_frames + inferred_duration_frames
            else:
                end_frames = sequence_duration_frames
    duration_frames = end_frames - start_frames
    ticks_per_second = 254_016_000_000
    in_seconds = int(ppro_ticks_in) / ticks_per_second if ppro_ticks_in else in_frames / fps
    out_seconds = int(ppro_ticks_out) / ticks_per_second if ppro_ticks_out else out_frames / fps

    _, volume_keyframes = extract_gain_data(clip_elem, clip_in_frames=in_frames, fps=fps)
    crossfade_in = False
    if previous_transition is not None:
        crossfade_in = (previous_transition.findtext("alignment") or "").strip().lower() == "center"
    visible_in_seconds = in_seconds
    visible_out_seconds = out_seconds
    if previous_transition is not None:
        previous_alignment = (previous_transition.findtext("alignment") or "").strip().lower()
        if previous_alignment == "center":
            # Premiere centered crossfades expose a later visible source start on
            # the incoming clip while still preserving the full hidden handle.
            visible_in_seconds += transition_duration_seconds(previous_transition, fps) / 2.0
    if next_transition is not None:
        next_alignment = (next_transition.findtext("alignment") or "").strip().lower()
        if next_alignment == "center":
            # Match Premiere's centered transition behavior by shortening the
            # outgoing clip's visible source window while keeping the hidden tail.
            visible_out_seconds -= transition_duration_seconds(next_transition, fps) / 2.0
    if visible_out_seconds < visible_in_seconds:
        visible_in_seconds = in_seconds
        visible_out_seconds = out_seconds

    return Clip(
        clip_id=clip_elem.attrib.get("id", ""),
        name=clip_elem.findtext("name") or "",
        source_track=source_track,
        file_id=file_id,
        path=path,
        start_frames=start_frames,
        end_frames=end_frames,
        in_frames=in_frames,
        out_frames=out_frames,
        duration_frames=duration_frames,
        start_seconds=start_frames / fps,
        end_seconds=end_frames / fps,
        in_seconds=in_seconds,
        out_seconds=out_seconds,
        visible_in_seconds=visible_in_seconds,
        visible_out_seconds=visible_out_seconds,
        enabled=parse_bool_text(clip_elem.findtext("enabled")),
        volume_level=1.0,
        fade_in_seconds=0.0,
        fade_out_seconds=0.0,
        crossfade_in=crossfade_in,
        volume_keyframes=volume_keyframes,
    )


def apply_transition_fades(track_elem: ET.Element, clips: List[Clip], fps: float) -> None:
    if not clips:
        return

    ordered = sorted(clips, key=lambda clip: (clip.start_frames, clip.end_frames, clip.clip_id))

    for transition_elem in track_elem.findall("transitionitem"):
        try:
            start_frames = int(transition_elem.findtext("start") or "0")
            end_frames = int(transition_elem.findtext("end") or "0")
        except ValueError:
            continue
        alignment = (transition_elem.findtext("alignment") or "").strip().lower()

        duration_frames = max(0, end_frames - start_frames)
        if duration_frames <= 0:
            continue
        duration_seconds = duration_frames / fps if fps else 0.0
        if duration_seconds <= 0:
            continue
        # Premiere centered crossfades sound closest in Ableton when the visible
        # source windows each contribute half the transition span, while the fade
        # length itself is also treated per-side instead of over the full overlap.
        fade_seconds = duration_seconds / 2.0 if alignment == "center" else duration_seconds

        before = None
        after = None
        for clip in ordered:
            if clip.end_frames <= end_frames:
                before = clip
            if clip.start_frames >= start_frames:
                after = clip
                break

        if before is not None and alignment in {"center", "end-black"}:
            before.fade_out_seconds = max(before.fade_out_seconds, fade_seconds)
        if after is not None and alignment in {"center", "start-black"}:
            after.fade_in_seconds = max(after.fade_in_seconds, fade_seconds)


def parse_timeline(path: Path) -> Timeline:
    root = read_xml(path)
    sequence = root.find("sequence")
    if sequence is None:
        raise ValueError("Expected Premiere xmeml with a top-level <sequence>")

    declared_fps = frame_rate_of(sequence)
    fps = infer_effective_frame_rate(sequence, declared_fps)
    duration_frames = int(sequence.findtext("duration") or "0")
    files = file_lookup(root)

    audio_tracks: List[AudioTrack] = []
    for index, track_elem in enumerate(sequence.findall("media/audio/track"), start=1):
        children = list(track_elem)
        clips = []
        for child_index, child in enumerate(children):
            if child.tag != "clipitem":
                continue

            previous_transition = next(
                (candidate for candidate in reversed(children[:child_index]) if candidate.tag == "transitionitem"),
                None,
            )
            next_transition = next(
                (candidate for candidate in children[child_index + 1 :] if candidate.tag == "transitionitem"),
                None,
            )
            clips.append(
                clip_from_xml(
                    child,
                    fps,
                    files,
                    duration_frames,
                    previous_transition=previous_transition,
                    next_transition=next_transition,
                )
            )
        apply_transition_fades(track_elem, clips, fps)
        audio_tracks.append(
            AudioTrack(
                index=index,
                clip_count=len(clips),
                enabled=parse_bool_text(track_elem.findtext("enabled")),
                muted=not parse_bool_text(track_elem.findtext("enabled")),
                volume_level=extract_gain_data(track_elem)[0],
                clips=clips,
            )
        )

    timeline = Timeline(
        sequence_name=sequence.findtext("name") or path.stem,
        frame_rate=fps,
        duration_frames=duration_frames,
        duration_seconds=duration_frames / fps if fps else 0.0,
        markers=parse_markers(sequence, fps),
        audio_tracks=dedupe_tracks(audio_tracks),
    )
    return timeline


def dedupe_tracks(audio_tracks: List[AudioTrack]) -> List[AudioTrack]:
    deduped: List[AudioTrack] = []
    seen_signatures = set()
    for track in audio_tracks:
        signature = tuple(
            (
                clip.name,
                clip.file_id,
                clip.path,
                clip.start_frames,
                clip.end_frames,
                round(clip.in_seconds, 6),
                round(clip.out_seconds, 6),
            )
            for clip in track.clips
        )
        if signature and signature in seen_signatures:
            continue
        if signature:
            seen_signatures.add(signature)
        deduped.append(track)

    for index, track in enumerate(deduped, start=1):
        track.index = index
        track.clip_count = len(track.clips)
    return deduped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse a Premiere/FCP7 xmeml export into a clean audio timeline model."
    )
    parser.add_argument("xml_path", type=Path, help="Path to a Premiere XML file")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    args = parser.parse_args()

    timeline = parse_timeline(args.xml_path)
    indent = 2 if args.pretty else None
    print(json.dumps(asdict(timeline), indent=indent))


if __name__ == "__main__":
    main()
