import base64
import math
import time
import uuid
from collections import defaultdict, deque

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

TOTAL = 53          # the fixed catalog: order IDs 1..53
RATE_LIMIT = 16     # requests allowed per client...
WINDOW = 10         # ...per this many seconds

app = FastAPI()

hits = defaultdict(deque)   # client id -> timestamps of its recent requests
orders_by_key = {}          # idempotency key -> the order we created


# ---------- 3) Rate limiter (runs on every request) ----------
@app.middleware("http")
async def rate_limit(request, call_next):
    client_id = request.headers.get("x-client-id")
    if client_id and request.method != "OPTIONS":
        now = time.monotonic()
        q = hits[client_id]

        # forget requests older than the window
        while q and now - q[0] >= WINDOW:
            q.popleft()

        if len(q) >= RATE_LIMIT:
            wait = max(1, math.ceil(WINDOW - (now - q[0])))
            return JSONResponse(
                {"detail": "Too many requests"},
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
        q.append(now)

    return await call_next(request)


# CORS is added AFTER the rate limiter, which makes it the outer layer,
# so even the 429 responses carry CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)


# ---------- 1) Idempotent order creation ----------
@app.post("/orders", status_code=201)
async def create_order(
    payload: dict | None = Body(default=None),
    idempotency_key: str | None = Header(default=None),
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header required")

    if idempotency_key not in orders_by_key:
        orders_by_key[idempotency_key] = {
            "id": f"ord_{uuid.uuid4().hex[:12]}",
            "status": "created",
            "payload": payload,
        }
    return orders_by_key[idempotency_key]


# ---------- 2) Cursor pagination ----------
def encode_cursor(n: int) -> str:
    return base64.urlsafe_b64encode(str(n).encode()).decode()


def decode_cursor(c: str) -> int:
    try:
        n = int(base64.urlsafe_b64decode(c.encode()).decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Bad cursor")
    if n < 0:
        raise HTTPException(status_code=400, detail="Bad cursor")
    return n


@app.get("/orders")
def list_orders(limit: int = 10, cursor: str | None = None):
    if limit < 1:
        raise HTTPException(status_code=422, detail="limit must be at least 1")

    start = decode_cursor(cursor) if cursor else 0     # how many we've already sent
    end = min(start + limit, TOTAL)

    items = [{"id": i, "name": f"Order {i}"} for i in range(start + 1, end + 1)]
    next_cursor = encode_cursor(end) if end < TOTAL else None

    return {"items": items, "next_cursor": next_cursor}