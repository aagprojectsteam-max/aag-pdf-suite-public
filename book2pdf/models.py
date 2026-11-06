from dataclasses import asdict, dataclass, field
from pathlib import Path
from . import __version__


class Unsupported(ValueError):
    """Evidence does not identify one safe reconstruction."""


@dataclass(frozen=True)
class Options:
    overwrite: bool = False
    skip_existing: bool = True
    validate_all_pages: bool = False
    jobs: int = 2
    recursive: bool = False
    preserve_tree: bool = True
    thermal_pause: bool = False
    pause_c: float = 85.0
    resume_c: float = 75.0
    recovery: str = "safe-salvage"

    def __post_init__(self):
        if not 1 <= self.jobs <= 8:
            raise ValueError("jobs must be between 1 and 8")
        if self.resume_c >= self.pause_c:
            raise ValueError("resume temperature must be below pause temperature")
        if self.recovery not in ('exact', 'safe-salvage'):
            raise ValueError('Unknown recovery mode')


@dataclass
class Result:
    source_path: str
    output_path: str
    source_size: int = 0
    source_mtime: int = 0
    source_hash: str = ""
    output_size: int = 0
    output_hash: str = ""
    detected_pdf_version: str = ""
    detected_wrapper_offset: int | None = None
    detected_startxref: int | None = None
    page_count: int = 0
    conversion_strategy: str = ""
    validation_mode: str = "representative"
    validation_status: str = "NOT_RUN"
    elapsed_seconds: float = 0
    conversion_seconds: float = 0
    validation_seconds: float = 0
    status: str = "FAIL_REPAIR"
    error: str = ""
    warning: str = ""
    timestamp: str = ""
    forensic: dict = field(default_factory=dict)
    recovery_class: str = "STRICT_EXACT_RECOVERY"
    reconstructed_objects: list = field(default_factory=list)
    preservation: dict = field(default_factory=dict)
    fidelity: str = "UNASSESSED"
    missing_data_synthesized: bool = False
    assumptions: list = field(default_factory=list)
    affected_pages: list = field(default_factory=list)
    dimensions: dict = field(default_factory=dict)
    recovered_document: dict = field(default_factory=dict)
    engine_version: str = __version__

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Job:
    source: Path
    output: Path
    error: str = ""
