"""Conservative HDR labels and bounded inspection of completed local media."""

import json
import os
import subprocess


def describe_format(fmt, youtube=False):
    transfer = str(fmt.get("color_transfer") or "").lower()
    label = str(fmt.get("dynamic_range") or "").upper()
    note = str(fmt.get("format_note") or "").upper()
    # A transfer function is stronger evidence than an extractor's itag label.
    if transfer == "arib-std-b67":
        return True, "HLG"
    if youtube:
        # YouTube's HDR itags are labelled HDR10 even for HLG and HDR10+.
        # Titles/format notes are not evidence of a particular HDR subtype.
        hdr = label not in ("", "SDR", "UNKNOWN") or "HDR" in note or transfer == "smpte2084"
        return hdr, "HDR" if hdr else "SDR"
    combined = f"{label} {note}"
    if "DOLBY VISION" in combined or label == "DV":
        return True, "Dolby Vision"
    if "HDR10+" in combined:
        return True, "HDR10+"
    if "HLG" in combined:
        return True, "HLG"
    if "HDR10" in combined or transfer == "smpte2084":
        return True, "HDR10"
    if label not in ("", "SDR", "UNKNOWN") or "HDR" in note:
        return True, "HDR"
    return False, "SDR"


def classify_probe(data):
    streams = data.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    frames = data.get("frames") or []
    side_data = list(stream.get("side_data_list") or [])
    for frame in frames:
        side_data.extend(frame.get("side_data_list") or [])
    kinds = {item.get("side_data_type", "").lower() for item in side_data}
    transfer = stream.get("color_transfer")
    label = None
    if any("dovi" in kind or "dolby vision" in kind for kind in kinds):
        label = "Dolby Vision"
    elif any("smpte2094-40" in kind or "hdr10+" in kind for kind in kinds):
        label = "HDR10+"
    elif transfer == "arib-std-b67":
        label = "HLG"
    elif transfer == "smpte2084":
        # A short sample cannot prove dynamic metadata is absent throughout.
        label = "HDR (PQ)"
    elif transfer in ("bt709", "smpte170m", "iec61966-2-1"):
        label = "SDR"
    if not label:
        return None
    return {
        "dynamic_range": label,
        "hdr": label != "SDR",
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": stream.get("codec_name"),
        "pixel_format": stream.get("pix_fmt"),
        "color_transfer": transfer,
        "color_primaries": stream.get("color_primaries"),
    }


def inspect_file(path, probes):
    """Inspect only a local finished file; missing/limited decoders never fail a download."""
    if not os.path.isfile(path):
        return None
    fallback = None
    for probe in list(dict.fromkeys(probes))[:2]:
        try:
            result = subprocess.run([
                probe, "-v", "error", "-select_streams", "v:0",
                "-read_intervals", "%+#48", "-show_streams", "-show_frames",
                "-show_entries",
                "stream=codec_name,width,height,pix_fmt,color_transfer,color_primaries:stream_side_data:frame_side_data",
                "-of", "json", os.path.abspath(path),
            ], capture_output=True, text=True, timeout=8)
            info = classify_probe(json.loads(result.stdout))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
        if info:
            fallback = info
            if info["dynamic_range"] in ("Dolby Vision", "HDR10+", "HLG", "SDR"):
                return info
            # Try a second decoder for AV1 when the bundled build cannot decode frames.
            if not result.stderr and result.returncode == 0:
                return info
    return fallback
