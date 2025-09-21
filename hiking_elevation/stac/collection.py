from pydantic import BaseModel


class STACCollection(BaseModel):
    id: str
    title: str
    extent: dict
