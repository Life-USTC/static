from pydantic import BaseModel, ConfigDict


class UpstreamBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UpstreamSnapshotModel(UpstreamBaseModel):
    """Store the complete JSON row alongside the typed SQLite projection."""
