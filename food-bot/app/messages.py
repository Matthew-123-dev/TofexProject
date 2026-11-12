"""Customer and rider message templates kept in one editable module."""

M1_CHOICE = (
    "Step 5 of 6: Delivery or Pickup?\n"
    "{cart_line}\n"
    "How would you like to receive your order?"
)
M2_STREET = (
    "✍️ *TYPE YOUR STREET ADDRESS & LANDMARK (Step 5 of 6)*\n"
    "Great, delivery it is! 🛵\n\n"
    "Please type your *house/plot number, street name, and nearest landmark* in one message (8–220 characters).\n"
    "👉 Example: *14 Denro-Ishashi Road, opposite St. Leo Catholic Church*\n\n"
    "📍 _Tip: You can also share your WhatsApp Location Pin right now!_\n"
    "_(Or reply *BACK* to change your area, or *CANCEL* to cancel)_"
)
M3_AREA = (
    "Step 5 of 6: Select your delivery area\n"
    "{cart_line}\n"
    "Pick your area to see the delivery fee & est. time:"
)
M3B_OTHER_AREA = (
    "✍️ *TYPE YOUR AREA NAME BELOW (Step 5 of 6)*\n"
    "No problem! Which area or neighborhood are you ordering from?\n"
    "👉 Example: *Ojodu Berger* or *Agbado Crossing*\n\n"
    "_(Or reply *BACK* to see our standard areas, or *CANCEL* to cancel)_"
)
M4_PIN = (
    "Step 5 of 6: Location pin (Optional)\n"
    "📍 To help our rider find you faster, you can share your WhatsApp location pin now, or skip this step."
)
M5_LANDMARK = (
    "✍️ *TYPE NEAREST LANDMARK BELOW (Step 5 of 6)*\n"
    "What is a popular landmark near your address?\n"
    "👉 Example: *Opposite St. Leo Catholic Church* or *Near Ajuwon Bus Stop*\n\n"
    "_(Or reply *BACK* to go back, or *CANCEL* to cancel)_"
)
M6_SECOND_PHONE = (
    "Step 5 of 6: Backup phone number (Optional)\n"
    "📞 If you have a second phone number for the rider, type it now, or tap an option below:"
)
M7_NOTES = (
    "Step 5 of 6: Delivery notes (Optional)\n"
    '📝 Any special instructions for the rider (e.g. "Call at the estate gate")? Type them now, or tap below:'
)
M8_SUMMARY = (
    "🧾 *ORDER SUMMARY (Step 6 of 6)*\n\n"
    "*Items:*\n{items_block}\n\n"
    "*Delivery Details:*\n"
    "📍 {street}, {area}\n"
    "🏛️ Landmark: {landmark}\n"
    "{second_phone_line}{notes_line}\n"
    "*Cost Breakdown:*\n"
    "• Items subtotal: {subtotal}\n"
    "• Delivery fee: {fee}\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "💰 *TOTAL TO PAY: {total}*"
)
M8_PICKUP_SUMMARY = (
    "🧾 *ORDER SUMMARY (Step 6 of 6)*\n\n"
    "*Items:*\n{items_block}\n\n"
    "*Pickup Location:*\n"
    "🏪 {outlet_address}\n\n"
    "*Cost Breakdown:*\n"
    "• Items subtotal: {subtotal}\n"
    "• Pickup fee: ₦0\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "💰 *TOTAL TO PAY: {total}*"
)
M8_CONFIRM_POLL = (
    "Step 6 of 6: Confirm your order\n"
    "💰 Total to pay: {total}\n"
    "Everything look right? Tap an option below:"
)
M8B_OTHER_AREA = (
    "⏳ *Checking Delivery Fee (Step 6 of 6)*\n"
    "Thank you! Your area isn't on our standard list, so our team is confirming your delivery fee right now. "
    "We'll message you shortly."
)
M8C_QUOTE = (
    "Step 6 of 6: Delivery fee quote ready\n"
    "📍 Area: {area} · Delivery fee: {fee}\n"
    "💰 Total to pay: {total}\n"
    "Would you like to proceed?"
)
M9_PAYMENT = (
    "✅ *Delivery Address Confirmed!*\n\n"
    "💰 *Total to pay:* {total}\n\n"
    "🏦 *Please transfer to:*\n"
    "• Bank: *{bank_name}*\n"
    "• Account No: *{account_number}*\n"
    "• Account Name: *{account_name}*\n\n"
    "📸 *Next Step:* Send a clear photo/screenshot of your transfer receipt here within *{minutes} minutes*.\n"
    "_(To update your street/landmark, reply *ADDRESS* · To cancel before paying, reply *CANCEL*)_"
)
M9_PICKUP = (
    "✅ *Pickup Order Confirmed!*\n"
    "🏪 Collect at: {outlet_address}\n\n"
    "💰 *Total to pay:* {total}\n\n"
    "🏦 *Please transfer to:*\n"
    "• Bank: *{bank_name}*\n"
    "• Account No: *{account_number}*\n"
    "• Account Name: *{account_name}*\n\n"
    "📸 *Next Step:* Send a clear photo/screenshot of your transfer receipt here within *{minutes} minutes*.\n"
    "_(To cancel before paying, reply *CANCEL*)_"
)
M10_RECEIPT = (
    "Receipt received. Thank you! 🙏 Our team is confirming your payment. "
    "Until we approve it, you can still change your delivery address within the same area by replying ADDRESS."
)
M11_APPROVED = (
    "Your payment is confirmed ✅ Thank you! Order #{batch} is now being prepared. "
    "We'll message you as soon as it's on its way."
)
M12_REJECTED = (
    "We couldn't verify your payment for order #{batch}. Please send a clear photo of your transfer receipt again, "
    "or call {support_phone} if you need help. Your delivery details are saved. You have {minutes} minutes to resend it."
)
M13_REMINDER = (
    "A friendly reminder: we're still waiting for {what} for order #{batch}. "
    "Please send it within {remaining} minutes, otherwise the order will be cancelled automatically."
)
M14_CANCELLED = (
    "Order #{batch} has been cancelled because we didn't receive {what} in time. "
    "You're welcome to start again whenever you're ready. Just send *menu*."
)
M15_LOCKED = (
    "Your order has already been approved, so the delivery address can no longer be changed. "
    "Please call {support_phone} and we'll help."
)
M15B_AREA_LOCKED = (
    "You can change your street, landmark and notes, but your area must stay the same now that your receipt has been sent. "
    "To change your area, please call {support_phone}."
)
M16_PICKUP_READY = (
    "Good news! Your order #{batch} is ready for pickup 🎉\n"
    "Collect it at: {outlet_address}\n"
    "Need directions? Call {outlet_phone}."
)
M17_ON_WAY = (
    "Your order #{batch} is on its way! 🛵\n"
    "Your rider is {rider_name}. You can reach them on {rider_phone}.\n"
    "Please keep your phone nearby."
)
M18_DELIVERED = (
    "Your order #{batch} has been delivered. Thank you for choosing {brand_name}. "
    "We hope you enjoy every bite! 🍽️\n"
    "If anything isn't right, tap the button below."
)
M18B_COLLECTED = "Thank you for collecting order #{batch}. We appreciate you choosing {brand_name}! 🍽️"
M19_NOT_RECEIVED = "Thank you for telling us. We're sorry for the trouble. Our team will contact you shortly."
M20_FAILED = (
    "We're very sorry, we couldn't deliver order #{batch} ({items}).\n"
    "Reason: {reasons}.\n"
    "This order is now closed. Our team will review your payment and contact you within {refund_timeframe} "
    "to confirm any refund due. To speak to us right away, call {support_phone}."
)
R1_DISPATCH = (
    "New delivery 🛵\nBatch #{batch} (orders {order_numbers})\nCustomer: {name}, {phone}\n"
    "Address: {street}, {area}\nLandmark: {landmark}\n{second_phone_line}{notes_line}"
    "Map: {map_link}\n{n} items across {m} order(s)"
)
R2_PICKED_UP = "Thanks! Tap Delivered once you've handed over batch #{batch}."
R3_CONFIRM_DELIVERED = "Confirm delivery of batch #{batch}?"
R3_DELIVERED = "Marked as delivered ✅ If this was a mistake, tap Undo within {minutes} minutes."
R4_PICKUP_REMINDER = "Reminder: batch #{batch} is waiting for pickup. Tap Picked up once you have collected it."
R5_HELP = "Commands:\nactive\ninactive\npending\npicked <batch>\ndelivered <batch>\nfail <batch>\nundo <batch>\nhelp"
R6_REASSIGNED = "Delivery #{batch} was reassigned. No action needed from you."
R7_WINDOW_WARNING = "Your availability expires in about an hour. Reply ACTIVE to stay available for deliveries."
R_ACTIVE = "You're now active ✅ You'll receive deliveries until you send INACTIVE."
R_INACTIVE = "You're now inactive. You won't receive new deliveries."
