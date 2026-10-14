"""令牌签发与刷新。"""

from utils.validators import validate_token


def issue_token(user_id):
    return f"tok-{user_id}"


def refresh_token(token):
    if validate_token(token):
        return token
    return None


def revoke_token(token):
    return True
