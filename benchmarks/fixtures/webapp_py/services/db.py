"""数据访问。"""


def connect(uri):
    return f"conn:{uri}"


def save_record(conn, record):
    return record


def persist(record):
    return save_record(None, record)


class Repository:
    def __init__(self, conn):
        self.conn = conn

    def save(self, record):
        return save_record(self.conn, record)
