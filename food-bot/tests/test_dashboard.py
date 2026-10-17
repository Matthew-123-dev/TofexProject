import pytest

from app.config import settings
from app.models import FulfillmentStage, MenuItem, OrderStatus
from app.services import order_service


@pytest.mark.asyncio
async def test_dashboard_approve_order(db, client, menu_item, batch, order):
    batch_id = batch.id
    menu_item_id = menu_item.id

    # 1. Authenticate as staff via session login
    login_response = await client.post(
        "/login",
        data={"username": settings.staff_username, "password": settings.staff_password},
        follow_redirects=False,
    )
    assert login_response.status_code == 303

    # 2. Rejecting the receipt returns the order to AWAITING_PAYMENT without cancelling it
    reject_response = await client.post(f"/api/staff/orders/{batch_id}/reject")
    assert reject_response.status_code == 200
    db.expunge_all()
    rejected_batch = await order_service.get_batch(db, batch_id)
    assert rejected_batch is not None
    assert rejected_batch.status == OrderStatus.PENDING
    assert rejected_batch.fulfillment.stage == FulfillmentStage.AWAITING_PAYMENT
    assert rejected_batch.receipt_media_id is None

    # Customer re-uploads a receipt so the order is back in AWAITING_APPROVAL
    rejected_batch.receipt_media_id = "receipt-media-2"
    rejected_batch.fulfillment.stage = FulfillmentStage.AWAITING_APPROVAL
    await db.commit()

    # 3. Approve the order -> marks batch & order PAID, moves stage to PREPARING, decrements stock
    approve_response = await client.post(f"/api/staff/orders/{batch_id}/approve")
    assert approve_response.status_code == 200

    db.expunge_all()
    approved_batch = await order_service.get_batch(db, batch_id)
    assert approved_batch is not None
    assert approved_batch.status == OrderStatus.PAID
    assert approved_batch.orders[0].status == OrderStatus.PAID
    assert approved_batch.fulfillment.stage == FulfillmentStage.PREPARING

    updated_item = await db.get(MenuItem, menu_item_id)
    assert updated_item is not None
    assert updated_item.stock_quantity == 4

    # 4. Approving an already-approved order returns 409 Conflict
    second_approve = await client.post(f"/api/staff/orders/{batch_id}/approve")
    assert second_approve.status_code == 409

    # 5. Deleting a paid order is refused with 409 Conflict
    delete_response = await client.delete(f"/api/staff/orders/{batch_id}")
    assert delete_response.status_code == 409

    # 6. After logging out, staff endpoints return 401 Unauthorized
    await client.post("/logout", follow_redirects=False)
    unauth_response = await client.get("/api/staff/orders")
    assert unauth_response.status_code == 401