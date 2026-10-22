"""Uniform frame sampling with PyAV (bundles ffmpeg; no system install needed).

Frames are cached on disk per (video, config) so repeated runs and cache lookups
don't pay the decode cost again.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from . import DATA_DIR


@dataclass(frozen=True)
class FrameCfg:
    """Frame sampling plus the one prompt knob that travels with it (option label style)."""
    n_frames: int = 8
    long_edge: int = 768
    jpeg_quality: int = 85
    label_style: str = "letter"   # "letter" -> (A) (B) (C); "number" -> (1) (2) (3)
    temperature: float | None = 0.0  # models that accept sampling params only; None sends none (API default)
    blind: bool = False           # no frames at all: question and options only (content-free prior)

    def key(self) -> str:
        return f"f{self.n_frames}-e{self.long_edge}-q{self.jpeg_quality}"


def _encode(frame, cfg: FrameCfg) -> bytes:
    from PIL import Image

    img: Image.Image = frame.to_image()
    img.thumbnail((cfg.long_edge, cfg.long_edge))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=cfg.jpeg_quality)
    return buf.getvalue()


def _sample_by_seek(video_path: Path, cfg: FrameCfg) -> list[bytes]:
    """Seek to n uniform timestamps; decode forward from the keyframe to the target."""
    import av

    out: list[bytes] = []
    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        if stream.duration is None or stream.time_base is None:
            raise ValueError("no duration")
        duration = int(stream.duration)
        targets = [int((i + 0.5) * duration / cfg.n_frames) for i in range(cfg.n_frames)]
        for target in targets:
            container.seek(target, stream=stream, backward=True, any_frame=False)
            chosen = None
            for frame in container.decode(stream):
                chosen = frame
                if frame.pts is not None and frame.pts >= target:
                    break
            if chosen is None:
                raise ValueError("no frame decoded")
            out.append(_encode(chosen, cfg))
    return out


def _sample_sequential(video_path: Path, cfg: FrameCfg) -> list[bytes]:
    """Fallback: decode everything, keep uniform positions. Two passes if the count is unknown."""
    import av

    def decode(keep: set[int] | None) -> tuple[list[bytes], int]:
        out: list[bytes] = []
        count = 0
        with av.open(str(video_path)) as container:
            stream = container.streams.video[0]
            for frame in container.decode(stream):
                if keep is not None and count in keep:
                    out.append(_encode(frame, cfg))
                count += 1
        return out, count

    _, total = decode(None)
    n = min(cfg.n_frames, total)
    keep = sorted({int((i + 0.5) * total / n) for i in range(n)})
    frames, _ = decode(set(keep))
    return frames


def sample_frames(video_path: Path, cfg: FrameCfg = FrameCfg(), cache_dir: Path = DATA_DIR / "frames") -> list[bytes]:
    """JPEG bytes for n frames spread uniformly over the video. Cached on disk."""
    d = cache_dir / video_path.parent.name / video_path.stem / cfg.key()
    expected = [d / f"{i:02d}.jpg" for i in range(cfg.n_frames)]
    if all(p.exists() for p in expected):
        return [p.read_bytes() for p in expected]

    try:
        frames = _sample_by_seek(video_path, cfg)
    except Exception:
        frames = _sample_sequential(video_path, cfg)
    if len(frames) != cfg.n_frames:
        frames = _sample_sequential(video_path, cfg)

    d.mkdir(parents=True, exist_ok=True)
    for i, jpg in enumerate(frames):
        (d / f"{i:02d}.jpg").write_bytes(jpg)
    return frames
