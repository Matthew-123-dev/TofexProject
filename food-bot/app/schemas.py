from dataclasses import dataclass


@dataclass(slots=True)
class IncomingMessage:
    """A WhatsApp message normalised into the fields our state machine needs."""

    id: str
    sender: str  # customer's phone number (wa_id)
    type: str  # text | interactive | image | ...
    text: str | None = None
    reply_id: str | None = None  # id of a tapped button or list row
    media_id: str | None = None  # WhatsApp media id (receipt photos)
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    location_address: str | None = None
