"""输入校验。"""


def validate_email(value):
    return "@" in value


def validate_token(token):
    return bool(token) and len(token) > 8
