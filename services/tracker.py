import json
import logging
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any, Dict, List, Union

import aioredis
import toml
from fastapi import FastAPI
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, conint, constr, stricturl
from starlette.config import Config
from starlette.requests import Request
from starlette_exporter import PrometheusMiddleware, handle_metrics

# Config
config = Config(".env")
REDIS_URL = config("REDIS_URL", cast=str)
ANNOUNCE_TTL = config("ANNOUNCE_TTL", cast=int, default=5 * 60)
PEER_LIMIT = config("PEER_LIMIT", cast=int, default=50)

logger = logging.getLogger(__name__)

# FastAPI init
app = FastAPI()
app.add_middleware(PrometheusMiddleware)
app.add_route("/metrics", handle_metrics)

# Prometheus metrics
UNIQUE_GROUPS = Gauge(
    "lvdiscovery_unique_groups", "Unique Librevault group count, seen on this tracker", multiprocess_mode="max",
)
UNIQUE_PEERS = Gauge(
    "lvdiscovery_unique_peers", "Unique Librevault peer count, seen on this tracker", multiprocess_mode="max",
)
REQUESTS_BY_CLIENT = Counter("lvdiscovery_requests_by_client", "User-Agent breakdown", ["ua"])

# Some constants
prefix = "lvdiscovery1:"
group_prefix = f"{prefix}group:"
stat_unique_groups = f"{prefix}statistics:unique_groups"
stat_unique_peers = f"{prefix}statistics:unique_peers"


class Announce(BaseModel):
    group_id: constr(max_length=128, regex=r"^(?:[0-9a-fA-F]{2})*$")  # sort of info_hash
    peer_id: constr(max_length=128, regex=r"^(?:[0-9a-fA-F]{2})*$")
    port: conint(gt=0, le=65535)


class Peer(BaseModel):
    peer_id: constr(max_length=128, regex=r"^(?:[0-9a-fA-F]{2})*$")
    url: stricturl(allowed_schemes={"ws", "wss"})


class AnnounceResponse(BaseModel):
    ttl: int
    peers: List[Peer]


class Deannounce(BaseModel):
    group_id: constr(max_length=128, regex=r"^(?:[0-9a-fA-F]{2})*$")  # sort of info_hash
    peer_id: constr(max_length=128, regex=r"^(?:[0-9a-fA-F]{2})*$")


@app.on_event("startup")
async def setup():
    app.redis_pool = await aioredis.create_redis_pool(REDIS_URL)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    REQUESTS_BY_CLIENT.labels(ua=request.headers["User-Agent"]).inc()
    response = await call_next(request)
    return response


def normalize_ip(ip: Union[str, IPv4Address, IPv6Address]):
    if isinstance(ip, str):
        ip = ip_address(ip)
    if isinstance(ip, IPv4Address):
        return ip
    return ip.ipv4_mapped if ip.ipv4_mapped else ip


@app.get("/v1/trackerinfo")
async def trackerinfo():
    with open("pyproject.toml", "r") as f:
        t = toml.load(f)
        return {"version": t["tool"]["poetry"]["version"]}


@app.post("/v1/announce", response_model=AnnounceResponse)
async def announce(ann: Announce, request: Request):
    ip = normalize_ip(request.client.host)
    group_id = bytes.fromhex(ann.group_id).hex()
    peer_id = bytes.fromhex(ann.peer_id).hex()

    peer_prefix = f"{group_prefix}{group_id}:"
    peer_key = f"{peer_prefix}{peer_id}"
    await app.redis_pool.set(
        peer_key, json.dumps({"peer_id": peer_id, "url": f"wss://{str(ip)}:{ann.port}/"}), expire=ANNOUNCE_TTL
    )

    # Response
    peers: List[Dict[str, Any]] = []
    cur = b"0"
    while cur:
        cur, keys = await app.redis_pool.scan(cur, match=f"{peer_prefix}*", count=PEER_LIMIT + 1)
        peers += [y for y in [json.loads(x) for x in await app.redis_pool.mget(*keys)] if y.get("peer_id") != peer_id]
    peers = peers[0:PEER_LIMIT]

    resp = {"ttl": ANNOUNCE_TTL, "peers": peers}

    # Update statistics
    await app.redis_pool.sadd(stat_unique_groups, bytes.fromhex(group_id))
    UNIQUE_GROUPS.set(await app.redis_pool.scard(stat_unique_groups))

    await app.redis_pool.sadd(stat_unique_peers, bytes.fromhex(peer_id))
    UNIQUE_PEERS.set(await app.redis_pool.scard(stat_unique_peers))

    return resp


@app.post("/v1/deannounce")
async def deannounce(ann: Deannounce):
    group_id = bytes.fromhex(ann.group_id).hex()
    peer_id = bytes.fromhex(ann.peer_id).hex()

    peer_prefix = f"{group_prefix}{group_id}:"
    peer_key = f"{peer_prefix}{peer_id}"
    await app.redis_pool.delete(peer_key)

    return {}
