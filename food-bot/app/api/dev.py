from fastapi import APIRouter

from app.dev_outbox import OUTBOX

router = APIRouter(prefix="/dev", tags=["dev"])


@router.get("/outbox")
async def read_outbox(clear: bool = True) -> list[dict]:
    """Messages the bot would have sent (dry-run mode only)."""
    items = list(OUTBOX)
    if clear:
        OUTBOX.clear()
    return items
