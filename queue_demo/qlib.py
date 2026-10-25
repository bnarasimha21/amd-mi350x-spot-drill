"""Shared queue primitives — ATOMIC lease/ack/reap via server-side Lua.

This code ships as reference material teaching "no ack, no loss", so the pattern must
actually survive kill -9 at any instruction boundary:
  lease: RPOP jobs + ZADD leases  — one atomic script (no lost-job window between them)
  ack:   ZREM leases (by the ORIGINAL raw string, never a re-serialization)
  reap:  expired leases move back to jobs — one atomic script (no loss, no duplication)
"""
import os
import redis

VISIBILITY_TIMEOUT_S = 120

_LEASE = """
local raw = redis.call('RPOP', KEYS[1])
if raw then redis.call('ZADD', KEYS[2], ARGV[1], raw) end
return raw
"""

_REAP = """
local moved = 0
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[1])
for _, raw in ipairs(expired) do
  redis.call('ZREM', KEYS[1], raw)
  redis.call('LPUSH', KEYS[2], raw)
  moved = moved + 1
end
return moved
"""


def client():
    for name in ("CONTROLLER_IP", "REDIS_PASSWORD"):
        if not os.environ.get(name):
            hint = (" Redis is opt-in: set ENABLE_REDIS=1 and REDIS_PASSWORD in .env, then re-run"
                    " provision/00_controller.sh.") if name == "REDIS_PASSWORD" else ""
            raise RuntimeError(f"{name} is not set.{hint}")
    return redis.Redis(host=os.environ["CONTROLLER_IP"], port=6379,
                       password=os.environ["REDIS_PASSWORD"], decode_responses=True)


class Queue:
    def __init__(self, r):
        self.r = r
        self._lease = r.register_script(_LEASE)
        self._reap = r.register_script(_REAP)

    def lease(self, now):
        return self._lease(keys=["jobs", "leases"], args=[now + VISIBILITY_TIMEOUT_S])

    def ack(self, raw):
        self.r.zrem("leases", raw)

    def reap(self, now):
        return self._reap(keys=["leases", "jobs"], args=[now])
