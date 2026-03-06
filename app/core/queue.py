from redis import Redis
from rq import Queue

from app.core.config import settings


redis_conn = Redis.from_url(settings.redis_url)
job_queue = Queue("downloads", connection=redis_conn)
