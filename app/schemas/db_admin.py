from pydantic import BaseModel, Field
from typing import Optional

class DBInstanceControlInput(BaseModel):
    oracle_home: str = Field(..., max_length=255)
    oracle_sid: str = Field(..., max_length=50, pattern="^[a-zA-Z0-9_]+$")
    action: str = Field(..., pattern="^(start|stop)$")

class SilentDBCARequestInput(BaseModel):
    oracle_home: str = Field(..., max_length=255)
    sid: str = Field(..., max_length=50, pattern="^[a-zA-Z0-9_]+$")
    global_db_name: str = Field(..., max_length=100)
    sys_password: str = Field(..., min_length=8)
    system_password: str = Field(..., min_length=8)
    total_memory_mb: int = Field(2048, ge=1024, le=65536)
    create_as_cdb: bool = True
    number_of_pdbs: int = Field(1, ge=1, le=50)
    pdb_name: Optional[str] = "pdb1"
    pdb_admin_password: Optional[str] = "DefaultPdbPass123!"
