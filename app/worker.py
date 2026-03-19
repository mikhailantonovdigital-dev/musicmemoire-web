from __future__ import annotations

from redis import Redis
from rq import Connection, Worker

from app.core.config import settings
from app.core.db import init_db


def main() -> None:
    init_db()
    connection = Redis.from_url(settings.REDIS_URL)
    with Connection(connection):
        worker = Worker([settings.BACKGROUND_QUEUE_NAME])
        worker.work()


if __name__ == "__main__":
    main()
