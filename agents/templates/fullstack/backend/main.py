"""
Ojas fullstack app — FastAPI backend entry point.

Conventions for Ojas fullstack apps:
  - One FastAPI app per project, mounted at "/" (NOT under /api/).
    Caddy strips the prefix, so the app sees clean paths.
  - All routes return JSON. The frontend (Vite) consumes these.
  - Database is SQLite at /opt/ojas-apps/<slug>/data/app.db (path
    passed via the DATABASE_URL env var in the systemd unit).
  - On boot: run Alembic migrations, then start uvicorn.
  - Always expose GET /health for the deploy health-check to poll.

If you're an LLM scaffolding a new app, copy this file and adjust:
  - The `items` model is an example — replace with your domain.
  - Keep /health as-is; Ojas's deploy pipeline uses it.
"""
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import Column, Integer, String, create_engine, select
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# ---- Database setup --------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
engine = create_engine(
    DATABASE_URL,
    # SQLite needs this when accessed from multiple threads (uvicorn workers
    # share the same process by default, so any thread can hit the DB).
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Item(Base):
    """Example model — a single 'items' table. Replace for your app."""
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    done = Column(Integer, default=0, nullable=False)  # 0/1 for SQLite simplicity


# Create tables on first boot. (For real migrations, use Alembic — see
# README.md in this directory.)
Base.metadata.create_all(bind=engine)


# ---- Pydantic schemas -----------------------------------------------------

class ItemIn(BaseModel):
    title: str
    done: bool = False


class ItemOut(BaseModel):
    id: int
    title: str
    done: bool

    class Config:
        from_attributes = True


# ---- App + lifespan -------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Anything to do on startup goes here. (For Alembic, run
    `alembic upgrade head` here instead of `create_all` above.)"""
    yield


app = FastAPI(title="Ojas fullstack app", lifespan=lifespan)

# CORS — only same-origin in production (Caddy proxies everything under
# one hostname), but allow the dev server (port 5180 etc.) too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- Routes ---------------------------------------------------------------
#
# IMPORTANT: define the APIRouter and ALL of its @router.* routes BEFORE
# calling app.include_router(). FastAPI's include_router() snapshots
# `router.routes` at the moment of the call (see
# `for route in router.routes:` in fastapi/routing.py), so routes added
# to the router afterwards are silently dropped. The order below matters.

api_router = APIRouter(prefix="/api")


@app.get("/health")
def health() -> dict:
    """Liveness probe. Ojas's deploy pipeline polls this for up to 5s
    after `systemctl start`. If it doesn't return 200, the deploy
    marks the app as 'error'."""
    return {"ok": True}


@api_router.get("/items", response_model=list[ItemOut])
def list_items(db: Session = Depends(get_db)) -> list[Item]:
    return list(db.execute(select(Item).order_by(Item.id)).scalars())


@api_router.post("/items", response_model=ItemOut, status_code=201)
def create_item(item: ItemIn, db: Session = Depends(get_db)) -> Item:
    obj = Item(title=item.title, done=1 if item.done else 0)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@api_router.patch("/items/{item_id}", response_model=ItemOut)
def update_item(item_id: int, item: ItemIn, db: Session = Depends(get_db)) -> Item:
    obj = db.get(Item, item_id)
    if obj is None:
        raise HTTPException(404, "item not found")
    obj.title = item.title
    obj.done = 1 if item.done else 0
    db.commit()
    db.refresh(obj)
    return obj


@api_router.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int, db: Session = Depends(get_db)) -> None:
    obj = db.get(Item, item_id)
    if obj is None:
        raise HTTPException(404, "item not found")
    db.delete(obj)
    db.commit()


# All routes must be defined on the router above this line.
app.include_router(api_router)
