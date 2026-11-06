"""Copy original bytes, changing only the PDF header and proven offset fields."""
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class Detection:
    """What is known about a PDF before it is written out."""
    strategy: str
    version: str
    wrapper_offset: int
    startxref: int
    body_start: int
    end: int
    patches: list = field(default_factory=list)
    header: bytes = b""


def reconstruct(source: Path, target: Path, detection: Detection):
    with source.open("rb") as src, target.open("wb") as dst:
        dst.write(detection.header)
        cursor = detection.body_start
        for start, end, replacement in sorted(detection.patches) + [(detection.end, detection.end, b"\n")]:
            if start < cursor:
                raise ValueError("Overlapping reconstruction patches")
            src.seek(cursor)
            remaining = start - cursor
            while remaining:
                block = src.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("Source changed or truncated during reconstruction")
                dst.write(block)
                remaining -= len(block)
            dst.write(replacement)
            cursor = end
