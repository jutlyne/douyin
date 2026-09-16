from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class LongDialogueLine:
    start: float
    end: float
    text_zh: str
    text_vi: str


@dataclass
class ChunkResult:
    index: int
    source_start: float
    source_end: float
    duration: float
    output_start: float = 0.0
    output_uri: str = ""
    summary_vi: str = ""
    lines: list[LongDialogueLine] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceResult:
    source_index: int
    douyin_url: str
    aweme_id: str
    duration: float
    output_uri: str
    metadata_uri: str
    subtitle_uri: str = ""
    title_vi: str = ""
    description_vi: str = ""
    chunks: list[ChunkResult] = field(default_factory=list)
    source_duration: float = 0.0
    source_start: float = 0.0
    source_processed_end: float = 0.0
    cliffhanger: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchItem:
    index: int
    output_uri: str
    metadata_uri: str = ""
    subtitle_uri: str = ""
    title_vi: str = ""
    duration: float = 0.0

    @classmethod
    def from_dict(cls, value: dict) -> "BatchItem":
        return cls(
            index=int(value["index"]),
            output_uri=str(value["output_uri"]),
            metadata_uri=str(value.get("metadata_uri") or ""),
            subtitle_uri=str(value.get("subtitle_uri") or ""),
            title_vi=str(value.get("title_vi") or ""),
            duration=float(value.get("duration") or 0.0),
        )
