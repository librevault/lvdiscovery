import json
import logging
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any, Dict, List, Union

import aioredis
from fastapi import FastAPI
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, IPvAnyAddress, conint, constr
from starlette.config import Config
from starlette.requests import Request
from starlette_exporter import PrometheusMiddleware, handle_metrics

# Config
config = Config(".env")
REDIS_URL = config("REDIS_URL", cast=str)
ANNOUNCE_TTL = config("ANNOUNCE_TTL", cast=int, default=15)
PEER_LIMIT = config("PEER_LIMIT", cast=int, default=50)

logger = logging.getLogger(__name__)

# FastAPI init
app = FastAPI()
app.add_middleware(PrometheusMiddleware)
app.add_route("/metrics", handle_metrics)

# Prometheus metrics
UNIQUE_COMMUNITIES = Gauge(
    "lvdiscovery_unique_communities",
    "Unique Librevault communities count, seen on this tracker",
    multiprocess_mode="max",
)
REQUESTS_BY_CLIENT = Counter("lvdiscovery_requests_by_client", "User-Agent breakdown", ["ua"])

# Some constants
prefix = "lvdiscovery1:"
community_prefix = f"{prefix}community:"
stat_unique_communities = f"{prefix}statistics:unique_communities"


class Announce(BaseModel):
    community_id: constr(max_length=50, regex=r"^(?:[0-9a-fA-F]{2})*$")  # sort of info_hash
    peer_id: constr(max_length=50, regex=r"^(?:[0-9a-fA-F]{2})*$")
    port: conint(le=65535)


class Peer(BaseModel):
    ip: Union[IPvAnyAddress]
    port: conint(le=65535)
    peer_id: constr(max_length=50, regex=r"^(?:[0-9a-fA-F]{2})*$")


class AnnounceResponse(BaseModel):
    ttl: int
    peers: List[Peer]


# class Deannounce(BaseModel):
#     community_id: str
#     peer_id: str


@app.on_event("startup")
async def setup():
    app.redis_pool = await aioredis.create_redis_pool(REDIS_URL)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    REQUESTS_BY_CLIENT.labels(ua=request.headers["User-Agent"])
    response = await call_next(request)
    return response


def normalize_ip(ip: Union[str, IPv4Address, IPv6Address]):
    if isinstance(ip, str):
        ip = ip_address(ip)
    if isinstance(ip, IPv4Address):
        return ip
    return ip.ipv4_mapped if ip.ipv4_mapped else ip


@app.post("/v1/announce", response_model=AnnounceResponse)
async def announce(ann: Announce, request: Request):
    ip = normalize_ip(request.client.host)
    community_id = bytes.fromhex(ann.community_id).hex()
    peer_id = bytes.fromhex(ann.peer_id).hex()

    peer_prefix = f"{community_prefix}{community_id}:"
    peer_key = f"{peer_prefix}{peer_id}"
    await app.redis_pool.set(
        peer_key, json.dumps({"ip": str(ip), "port": ann.port, "peer_id": peer_id}), expire=ANNOUNCE_TTL
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
    if await app.redis_pool.sadd(stat_unique_communities, bytes.fromhex(community_id)):
        UNIQUE_COMMUNITIES.set(await app.redis_pool.scard(stat_unique_communities))

    return resp
