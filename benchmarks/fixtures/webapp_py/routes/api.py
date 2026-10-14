"""对外 API 路由。"""

from auth.middleware import authenticate
from auth.service import AuthService
from services.db import Repository, connect
from utils.logger import get_logger


def login_route(payload):
    authenticate(payload)
    service = AuthService(get_logger("api"))
    repo = Repository(connect("db://main"))
    repo.save(payload)
    return service.login(payload["email"], payload["password"])


def refresh_route(payload):
    service = AuthService(get_logger("api"))
    return service.refresh(payload["token"])
