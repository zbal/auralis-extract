from rq import Worker

from app.core.queue import redis_conn


if __name__ == "__main__":
    worker = Worker(["downloads"], connection=redis_conn)
    worker.work()
