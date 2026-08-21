from pydantic import BaseModel, Field

class ServiceControlInput(BaseModel):
    service_name: str = Field(..., min_length=2, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    action: str = Field(..., min_length=4, max_length=7, pattern="^(start|stop|restart|status)$")

class ServiceControlResponse(BaseModel):
    service: str
    action: str
    execution_state: str
    system_output: str
