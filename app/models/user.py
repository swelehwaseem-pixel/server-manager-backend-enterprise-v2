from sqlalchemy import Column, String
from app.models.role import Role
# ... your existing imports (Base, etc.)

class User(Base):
    __tablename__ = "users"
    # ... your existing columns (id, username, hashed_password, etc.)

    role: str = Column(String, default=Role.VIEWER.value, nullable=False)
