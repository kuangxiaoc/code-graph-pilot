"""认证服务。"""

from auth.tokens import issue_token, refresh_token
from services.db import persist
from utils.validators import validate_email


class BaseService:
    def __init__(self, logger):
        self.logger = logger

    def audit(self, action):
        return action


class AuthService(BaseService):
    def login(self, email, password):
        self.audit("login")
        if not validate_email(email):
            return None
        persist({"email": email})
        return issue_token(email)

    def refresh(self, token):
        return refresh_token(token)
