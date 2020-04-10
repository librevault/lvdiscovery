import json
import logging
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any, Dict, List, Union

import aioredis
from fastapi import FastAPI
from pydantic import BaseModel, IPvAnyAddress, conint, constr
from starlette.config import Config
from starlette.requests import Request

config = Config(".env")
REDIS_URL = config("REDIS_URL", cast=str)
ANNOUNCE_TTL = config("ANNOUNCE_TTL", cast=int, default=15)
PEER_LIMIT = config("PEER_LIMIT", cast=int, default=50)

logger = logging.getLogger(__name__)

app = FastAPI()

prefix = "lvdiscovery1:"
community_prefix = f"{prefix}community:"


class Announce(BaseModel):
    community_id: constr(max_length=50)  # sort of info_hash
    peer_id: constr(max_length=50, regex=r"^[0-9a-fA-F]*$")
    port: conint(le=65535)


class Peer(BaseModel):
    ip: Union[IPvAnyAddress]
    port: conint(le=65535)
    peer_id: constr(max_length=50, regex=r"^[0-9a-fA-F]*$")


class AnnounceResponse(BaseModel):
    ttl: int
    peers: List[Peer]


# class Deannounce(BaseModel):
#     community_id: str
#     peer_id: str


@app.on_event("startup")
async def setup():
    app.redis_pool = await aioredis.create_redis_pool(REDIS_URL)


def normalize_ip(ip: Union[str, IPv4Address, IPv6Address]):
    if isinstance(ip, str):
        ip = ip_address(ip)
    if isinstance(ip, IPv4Address):
        return ip
    return ip.ipv4_mapped if ip.ipv4_mapped else ip


@app.post("/v1/announce", response_model=AnnounceResponse)
async def announce(ann: Announce, request: Request):
    ip = normalize_ip(request.client.host)

    peer_prefix = f"{community_prefix}{ann.community_id}:"
    peer_key = f"{peer_prefix}{ann.peer_id}"
    await app.redis_pool.set(
        peer_key, json.dumps({"ip": str(ip), "port": ann.port, "peer_id": ann.peer_id}), expire=ANNOUNCE_TTL
    )

    peers: List[Dict[str, Any]] = []
    cur = b"0"
    while cur:
        cur, keys = await app.redis_pool.scan(cur, match=f"{peer_prefix}*", count=PEER_LIMIT + 1)
        peers += [
            y for y in [json.loads(x) for x in await app.redis_pool.mget(*keys)] if y.get("peer_id") != ann.peer_id
        ]
    peers = peers[0:PEER_LIMIT]

    # Response
    resp = {"ttl": ANNOUNCE_TTL, "peers": peers}

    return resp
