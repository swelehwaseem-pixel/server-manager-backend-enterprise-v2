from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
)

from app.database import Base
from app.models.role import Role


class User(Base):
    __tablename__ = "users"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    username = Column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )

    hashed_password = Column(
        String(255),
        nullable=False,
    )

    role = Column(
        String(50),
        default=Role.VIEWER.value,
        nullable=False,
    )

    mfa_enabled = Column(
        Boolean,
        default=False,
        nullable=False,
    )

    mfa_secret = Column(
        String(255),
        nullable=True,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    def __repr__(self):
        return (
            f"<User("
            f"id={self.id}, "
            f"username='{self.username}', "
            f"role='{self.role}'"
            f")>"
        )
