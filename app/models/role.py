from enum import Enum


class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    DBA = "dba"
    ADMIN = "admin"


# Defines what each role includes — higher roles inherit lower ones
ROLE_HIERARCHY = {
    Role.VIEWER: {Role.VIEWER},
    Role.OPERATOR: {Role.VIEWER, Role.OPERATOR},
    Role.DBA: {Role.VIEWER, Role.OPERATOR, Role.DBA},
    Role.ADMIN: {Role.VIEWER, Role.OPERATOR, Role.DBA, Role.ADMIN},
}
