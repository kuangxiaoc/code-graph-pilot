"""日志工具。"""


def get_logger(name):
    return f"logger:{name}"


def log_event(logger, message):
    return f"{logger} -> {message}"
