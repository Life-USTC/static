from pydantic import BaseModel, ConfigDict


class UpstreamBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
