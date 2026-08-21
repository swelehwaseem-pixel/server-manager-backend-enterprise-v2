from pydantic import BaseModel, Field

class TokenResponse(BaseModel):
    access_token: str
    token_type: str

class UserProfileResponse(BaseModel):
    id: int
    username: str

    model_config = {"from_attributes": True}
