"""请求认证中间件。"""

from auth.service import AuthService
from utils.logger import get_logger
from utils.validators import validate_token


def authenticate(request):
    validate_token(request.get("token"))
    return True


def wrapper(handler):
    logger = get_logger("middleware")
    AuthService(logger)
    return handler
