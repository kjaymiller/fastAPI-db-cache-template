from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, ForeignKey, String, select
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from valkey.asyncio import Valkey

GUESTBOOKS_CACHE_KEY = "guestbooks:list"


def guestbook_cache_key(slug: str) -> str:
    return f"guestbook:{slug}:recent"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("name must contain at least one letter or number")
    return slug


class Settings(BaseSettings):
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    valkey_host: str = "localhost"
    valkey_port: int = 6379

    cache_ttl_seconds: int = 900

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Guestbook(Base):
    __tablename__ = "guestbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    entries: Mapped[list[Entry]] = relationship(back_populates="guestbook")


class Entry(Base):
    __tablename__ = "guestbook_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    guestbook_id: Mapped[int] = mapped_column(ForeignKey("guestbooks.id"))
    name: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    guestbook: Mapped[Guestbook] = relationship(back_populates="entries")


settings = Settings()
engine = create_async_engine(settings.database_url)
Session = async_sessionmaker(engine, expire_on_commit=False)
cache = Valkey(host=settings.valkey_host, port=settings.valkey_port, decode_responses=True)
templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await cache.aclose()
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


async def list_guestbooks() -> list[dict]:
    cached = await cache.get(GUESTBOOKS_CACHE_KEY)
    if cached:
        return json.loads(cached)

    async with Session() as session:
        result = await session.execute(
            select(Guestbook).order_by(Guestbook.created_at.desc())
        )
        guestbooks = [
            {"slug": g.slug, "name": g.name} for g in result.scalars()
        ]

    await cache.set(GUESTBOOKS_CACHE_KEY, json.dumps(guestbooks), ex=settings.cache_ttl_seconds)
    return guestbooks


async def recent_entries(slug: str) -> list[dict]:
    cache_key = guestbook_cache_key(slug)
    cached = await cache.get(cache_key)
    if cached:
        return json.loads(cached)

    async with Session() as session:
        result = await session.execute(
            select(Entry)
            .join(Guestbook)
            .where(Guestbook.slug == slug)
            .order_by(Entry.created_at.desc())
            .limit(20)
        )
        entries = [
            {
                "name": e.name,
                "message": e.message,
                "created_at": e.created_at.strftime("%Y-%m-%d %H:%M"),
            }
            for e in result.scalars()
        ]

    await cache.set(cache_key, json.dumps(entries), ex=settings.cache_ttl_seconds)
    return entries


@app.get("/")
async def index(request: Request):
    guestbooks = await list_guestbooks()
    return templates.TemplateResponse(
        request, "index.html", {"guestbooks": guestbooks}
    )


@app.post("/guestbooks")
async def create_guestbook(name: str = Form(...)):
    name = name.strip()[:80]
    try:
        slug = slugify(name)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid guestbook name") from err

    async with Session() as session:
        existing = await session.scalar(select(Guestbook).where(Guestbook.slug == slug))
        if existing is None:
            session.add(Guestbook(slug=slug, name=name))
            await session.commit()

    await cache.delete(GUESTBOOKS_CACHE_KEY)
    return RedirectResponse(f"/guestbooks/{slug}", status_code=303)


@app.get("/guestbooks/{slug}")
async def show_guestbook(request: Request, slug: str):
    async with Session() as session:
        guestbook = await session.scalar(select(Guestbook).where(Guestbook.slug == slug))
    if guestbook is None:
        raise HTTPException(status_code=404, detail="Guestbook not found")

    entries = await recent_entries(slug)
    return templates.TemplateResponse(
        request,
        "guestbook.html",
        {"guestbook": guestbook, "entries": entries},
    )


@app.post("/guestbooks/{slug}/entries")
async def create_entry(slug: str, name: str = Form(...), message: str = Form(...)):
    async with Session() as session:
        guestbook = await session.scalar(select(Guestbook).where(Guestbook.slug == slug))
        if guestbook is None:
            raise HTTPException(status_code=404, detail="Guestbook not found")

        session.add(
            Entry(
                guestbook_id=guestbook.id,
                name=name.strip()[:80],
                message=message.strip()[:500],
            )
        )
        await session.commit()

    await cache.delete(guestbook_cache_key(slug))
    return RedirectResponse(f"/guestbooks/{slug}", status_code=303)
