"""Create the current development schema and synchronize the menu."""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
import app.models  # noqa: F401
from app.database import Base, engine
from app.seed import main as seed


async def main() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed()


if __name__ == "__main__":
    asyncio.run(main())
