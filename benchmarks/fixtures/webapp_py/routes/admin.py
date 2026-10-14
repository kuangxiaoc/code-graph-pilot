"""管理后台路由。"""

from auth.service import AuthService
from services.db import persist
from utils.logger import get_logger


def admin_route(payload):
    service = AuthService(get_logger("admin"))
    persist(payload)
    return service.audit("admin")
