"""Create database tables for local development.

Run from the project root:

    python init_db.py

This uses the async engine in app.database and runs Base.metadata.create_all
in a synchronous context via run_sync.
"""
import asyncio

from app.database import engine, Base


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables created")


if __name__ == "__main__":
    asyncio.run(main())
