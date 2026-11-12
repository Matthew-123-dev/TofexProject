"""Rule-based + RapidFuzz natural-language order parser for Tofex Grill & More.

Zero external API cost (₦0/msg) and ~5 MB memory footprint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import logging
import re
from typing import Sequence

try:
    from rapidfuzz import fuzz, process as rf_process
except ImportError:  # pragma: no cover - fallback if rapidfuzz is not installed
    from difflib import SequenceMatcher

    class _FallbackFuzz:
        @staticmethod
        def token_sort_ratio(a: str, b: str) -> float:
            sa = " ".join(sorted(a.lower().split()))
            sb = " ".join(sorted(b.lower().split()))
            return SequenceMatcher(None, sa, sb).ratio() * 100.0

    fuzz = _FallbackFuzz()  # type: ignore[assignment]
    rf_process = None

from app.models import MenuItem
from app.services import menu_service
from app.utils import naira

logger = logging.getLogger(__name__)

MAX_FOLLOWUP_POLLS = 2
HIGH_CONFIDENCE_SCORE = 85.0
MEDIUM_CONFIDENCE_SCORE = 70.0

NUMBER_WORDS: dict[str, int] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "single": 1,
    "two": 2,
    "couple": 2,
    "double": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "dozen": 12,
}

# Unit words to strip when they sit between a quantity and an item name (not "can" or "plastic" before "malt")
UNIT_WORDS = {
    "plate", "plates", "portion", "portions", "wrap", "wraps", "roll", "rolls",
    "piece", "pieces", "pc", "pcs", "cup", "cups", "tub", "tubs", "bottle", "bottles",
    "pack", "packs", "takeaway", "bowl", "bowls", "serving", "servings", "scoop", "scoops",
}

POLITE_FILLER_RE = re.compile(
    r"^\s*(?:please|pls|abeg|hi|hello|hey|good\s+(?:morning|afternoon|evening)|"
    r"i\s+want\s+to\s+order|i\s+would\s+like\s+to\s+order|i\s+would\s+like|"
    r"i\s+want|i\s+need|can\s+i\s+get|can\s+i\s+have|give\s+me|let\s+me\s+get|"
    r"let\s+me\s+have|send\s+me|pack\s+for\s+me|order\s+for\s+me|order)\b[\s,:.-]*",
    re.IGNORECASE,
)
TRAILING_FILLER_RE = re.compile(
    r"[\s,.!]*(?:please|pls|abeg|thank\s+you|thanks|thx)\s*$",
    re.IGNORECASE,
)

# Canonical alias dictionary mapping local slang, sizes, and price shorthands to exact DB item names
ITEM_ALIASES: dict[str, str] = {
    # Rice, Spaghetti & Porridge
    "jollof": "Jollof rice",
    "jolof": "Jollof rice",
    "jellof": "Jollof rice",
    "jollof rice": "Jollof rice",
    "jolof rice": "Jollof rice",
    "jellof rice": "Jollof rice",
    "party jollof": "Jollof rice",
    "fried rice": "Fried rice",
    "friedrice": "Fried rice",
    "spag": "Spaghetti",
    "spags": "Spaghetti",
    "spaghetti": "Spaghetti",
    "spagetti": "Spaghetti",
    "pasta": "Spaghetti",
    "porridge": "Porridge",
    "yam porridge": "Porridge",
    "asaro": "Porridge",
    "pottage": "Porridge",
    # Beef, Fish, Plantain & Extras
    "dodo": "Plantain (1)",
    "plantain": "Plantain (1)",
    "plantains": "Plantain (1)",
    "fried plantain": "Plantain (1)",
    "plantain 1": "Plantain (1)",
    "moi moi": "Moimoi",
    "moimoi": "Moimoi",
    "moi-moi": "Moimoi",
    "mion mion": "Moimoi",
    "bean pudding": "Moimoi",
    "beef": "Beef",
    "meat": "Beef",
    "egg": "Egg",
    "eggs": "Egg",
    "boiled egg": "Egg",
    "ponmo": "Ponmo",
    "pomo": "Ponmo",
    "kpomo": "Ponmo",
    "kponmo": "Ponmo",
    "cow skin": "Ponmo",
    "salad": "Salad",
    "coleslaw": "Salad",
    "veggie salad": "Salad",
    "medium fish": "Fish (Medium)",
    "fish medium": "Fish (Medium)",
    "small fish": "Fish (Medium)",
    "fish small": "Fish (Medium)",
    "fish 1500": "Fish (Medium)",
    "big fish": "Fish (Big)",
    "large fish": "Fish (Big)",
    "fish big": "Fish (Big)",
    "fish large": "Fish (Big)",
    "fish 2000": "Fish (Big)",
    # Grilled Chicken (1..5)
    "small chicken": "Chicken (1)",
    "chicken small": "Chicken (1)",
    "chicken 1": "Chicken (1)",
    "chicken (1)": "Chicken (1)",
    "chicken 2500": "Chicken (1)",
    "medium chicken": "Chicken (2)",
    "chicken medium": "Chicken (2)",
    "chicken 2": "Chicken (2)",
    "chicken (2)": "Chicken (2)",
    "chicken 3000": "Chicken (2)",
    "large chicken": "Chicken (3)",
    "big chicken": "Chicken (3)",
    "chicken large": "Chicken (3)",
    "chicken big": "Chicken (3)",
    "chicken 3": "Chicken (3)",
    "chicken (3)": "Chicken (3)",
    "chicken 3500": "Chicken (3)",
    "xl chicken": "Chicken (4)",
    "extra large chicken": "Chicken (4)",
    "xtralarge chicken": "Chicken (4)",
    "chicken xl": "Chicken (4)",
    "chicken extra large": "Chicken (4)",
    "chicken 4": "Chicken (4)",
    "chicken (4)": "Chicken (4)",
    "chicken 4000": "Chicken (4)",
    "xxl chicken": "Chicken (5)",
    "xxtralarge chicken": "Chicken (5)",
    "chicken xxl": "Chicken (5)",
    "chicken 5": "Chicken (5)",
    "chicken (5)": "Chicken (5)",
    "chicken 4500": "Chicken (5)",
    # Grilled Turkey (1..5)
    "small turkey": "Turkey (1)",
    "turkey small": "Turkey (1)",
    "turkey 1": "Turkey (1)",
    "turkey (1)": "Turkey (1)",
    "turkey 3000": "Turkey (1)",
    "medium turkey": "Turkey (2)",
    "turkey medium": "Turkey (2)",
    "turkey 2": "Turkey (2)",
    "turkey (2)": "Turkey (2)",
    "turkey 3500": "Turkey (2)",
    "large turkey": "Turkey (3)",
    "big turkey": "Turkey (3)",
    "turkey large": "Turkey (3)",
    "turkey big": "Turkey (3)",
    "turkey 3": "Turkey (3)",
    "turkey (3)": "Turkey (3)",
    "turkey 4000": "Turkey (3)",
    "xl turkey": "Turkey (4)",
    "extra large turkey": "Turkey (4)",
    "xtralarge turkey": "Turkey (4)",
    "turkey xl": "Turkey (4)",
    "turkey extra large": "Turkey (4)",
    "turkey 4": "Turkey (4)",
    "turkey (4)": "Turkey (4)",
    "turkey 4500": "Turkey (4)",
    "xxl turkey": "Turkey (5)",
    "xxtralarge turkey": "Turkey (5)",
    "turkey xxl": "Turkey (5)",
    "turkey 5": "Turkey (5)",
    "turkey (5)": "Turkey (5)",
    "turkey 5000": "Turkey (5)",
    # Shawarma & Snacks
    "meatpie": "Meatpie",
    "meat pie": "Meatpie",
    "pie": "Meatpie",
    "eggroll": "Eggroll",
    "egg roll": "Eggroll",
    "egg-roll": "Eggroll",
    "regular shawarma": "Shawama (1200)",
    "small shawarma": "Shawama (1200)",
    "shawarma regular": "Shawama (1200)",
    "shawarma small": "Shawama (1200)",
    "shawarma 1200": "Shawama (1200)",
    "sharwama 1200": "Shawama (1200)",
    "shawama 1200": "Shawama (1200)",
    "shawama (1200)": "Shawama (1200)",
    "sharwama regular": "Shawama (1200)",
    "shawama regular": "Shawama (1200)",
    "large shawarma": "Shawama (2000)",
    "big shawarma": "Shawama (2000)",
    "jumbo shawarma": "Shawama (2000)",
    "shawarma large": "Shawama (2000)",
    "shawarma big": "Shawama (2000)",
    "shawarma 2000": "Shawama (2000)",
    "sharwama 2000": "Shawama (2000)",
    "shawama 2000": "Shawama (2000)",
    "shawama (2000)": "Shawama (2000)",
    "sharwama large": "Shawama (2000)",
    "shawama large": "Shawama (2000)",
    "small popcorn": "Popcorn (200)",
    "popcorn small": "Popcorn (200)",
    "popcorn 200": "Popcorn (200)",
    "popcorn (200)": "Popcorn (200)",
    "large popcorn": "Popcorn (300)",
    "big popcorn": "Popcorn (300)",
    "popcorn large": "Popcorn (300)",
    "popcorn big": "Popcorn (300)",
    "popcorn 300": "Popcorn (300)",
    "popcorn (300)": "Popcorn (300)",
    # Ice Cream
    "small cup": "Small cup",
    "small ice cream": "Small cup",
    "small icecream": "Small cup",
    "ice cream small": "Small cup",
    "ice cream 400": "Small cup",
    "medium cup": "Medium",
    "medium ice cream": "Medium",
    "medium icecream": "Medium",
    "ice cream medium": "Medium",
    "ice cream 600": "Medium",
    "big cup": "Big cup",
    "large cup": "Big cup",
    "big ice cream": "Big cup",
    "large ice cream": "Big cup",
    "ice cream big": "Big cup",
    "ice cream large": "Big cup",
    "ice cream 800": "Big cup",
    "1/2 liter": "1/2 liter",
    "1/2 litre": "1/2 liter",
    "half liter": "1/2 liter",
    "half litre": "1/2 liter",
    "half liter ice cream": "1/2 liter",
    "half litre ice cream": "1/2 liter",
    "ice cream tub": "1/2 liter",
    "ice cream 2000": "1/2 liter",
    # Cold Drinks
    "coke": "Coke",
    "coca cola": "Coke",
    "coca-cola": "Coke",
    "fanta": "Fanta",
    "sprite": "Sprite",
    "pepsi": "Pepsi",
    "cola": "Cola",
    "american cola": "Cola",
    "water": "Bottle water",
    "bottle water": "Bottle water",
    "bottled water": "Bottle water",
    "table water": "Bottle water",
    "eva water": "Bottle water",
    "hollandia": "Hollandia",
    "hollandia yoghurt": "Hollandia",
    "exotic": "Exotic",
    "chi exotic": "Exotic",
    "active": "Active",
    "chivita": "Active",
    "chivita active": "Active",
    "pulpy": "Pulpy orange",
    "pulpy orange": "Pulpy orange",
    "5 alive": "Pulpy orange",
    "five alive": "Pulpy orange",
    "berryblast": "Berryblast",
    "berry blast": "Berryblast",
    "can malt": "Can malt",
    "canned malt": "Can malt",
    "malt can": "Can malt",
    "plastic malt": "Plastic malt",
    "bottle malt": "Plastic malt",
    "malt bottle": "Plastic malt",
    "malt plastic": "Plastic malt",
    "freshyo": "Freshyo",
    "fresh yo": "Freshyo",
    "vijuchoco": "Vijuchoco",
    "viju choco": "Vijuchoco",
    "viju": "Vijuchoco",
    "nutrichoco": "Nutrichoco",
    "nutri choco": "Nutrichoco",
    "nutrimilk": "Nutrimilk",
    "nutri milk": "Nutrimilk",
    "fearless": "Fearless",
    "fearless energy": "Fearless",
    "lucozade": "Lucozade",
    "lucozade boost": "Lucozade",
}

# Ambiguous size families: bare keyword -> (Display family name, ordered DB item names, example to type)
SIZE_FAMILIES: dict[str, tuple[str, list[str], str]] = {
    "chicken": (
        "Grilled Chicken",
        ["Chicken (1)", "Chicken (2)", "Chicken (3)", "Chicken (4)", "Chicken (5)"],
        "1 chicken medium",
    ),
    "turkey": (
        "Grilled Turkey",
        ["Turkey (1)", "Turkey (2)", "Turkey (3)", "Turkey (4)", "Turkey (5)"],
        "1 turkey large",
    ),
    "shawarma": (
        "Shawarma",
        ["Shawama (1200)", "Shawama (2000)"],
        "1 shawarma large",
    ),
    "sharwama": (
        "Shawarma",
        ["Shawama (1200)", "Shawama (2000)"],
        "1 shawarma large",
    ),
    "shawama": (
        "Shawarma",
        ["Shawama (1200)", "Shawama (2000)"],
        "1 shawarma large",
    ),
    "fish": (
        "Fish",
        ["Fish (Medium)", "Fish (Big)"],
        "1 fish big",
    ),
    "popcorn": (
        "Popcorn",
        ["Popcorn (200)", "Popcorn (300)"],
        "1 popcorn large",
    ),
    "ice cream": (
        "Ice Cream",
        ["Small cup", "Medium", "Big cup", "1/2 liter"],
        "1 medium ice cream",
    ),
    "icecream": (
        "Ice Cream",
        ["Small cup", "Medium", "Big cup", "1/2 liter"],
        "1 medium ice cream",
    ),
    "malt": (
        "Malt Drink",
        ["Can malt", "Plastic malt"],
        "1 can malt",
    ),
}

# Special request patterns (item-specific & general)
SPECIAL_REQUEST_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Rice-specific: add stew
    (re.compile(r"\b(?:add|extra|more|with|plenty)\s+stew\b", re.IGNORECASE), "add stew"),
    # Shawarma-specific: sausage & pepper
    (re.compile(r"\b(?:no|without|remove|minus)\s+sausages?\b", re.IGNORECASE), "no sausage"),
    (re.compile(r"\b(?:extra|more|double)\s+sausages?\b", re.IGNORECASE), "extra sausage"),
    (re.compile(r"\b(?:extra|more|plenty|lots\s+of)\s+pepper\b", re.IGNORECASE), "extra pepper"),
    (re.compile(r"\b(?:less|little|small|light|not\s+much)\s+pepper\b", re.IGNORECASE), "less pepper"),
    (re.compile(r"\b(?:no|without|zero)\s+pepper\b", re.IGNORECASE), "no pepper"),
    # General kitchen modifiers
    (re.compile(r"\bwell\s+done\b", re.IGNORECASE), "well done"),
    (re.compile(r"\b(?:no|without)\s+onions?\b", re.IGNORECASE), "no onions"),
    (re.compile(r"\b(?:no|without)\s+mayo(?:nnaise)?\b", re.IGNORECASE), "no mayo"),
    (re.compile(r"\b(?:no|without)\s+cream\b", re.IGNORECASE), "no cream"),
    (re.compile(r"\b(?:less|little)\s+cream\b", re.IGNORECASE), "less cream"),
    (re.compile(r"\b(?:extra|more)\s+cream\b", re.IGNORECASE), "extra cream"),
    (re.compile(r"\b(?:very|extra)\s+(?:hot|spicy)\b", re.IGNORECASE), "extra spicy"),
]


@dataclass
class MatchedOrderItem:
    item_id: int
    name: str
    display_name: str
    unit_price: Decimal
    quantity: int
    notes: list[str] = field(default_factory=list)
    stock_capped_from: int | None = None

    @property
    def subtotal(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass
class FollowUpQuestion:
    kind: str  # "size" | "fuzzy" | "naira_mismatch"
    raw_phrase: str
    quantity: int
    family_label: str
    options: list[tuple[int, str, Decimal]]  # (item_id, display_name, unit_price)
    example_hint: str
    notes: list[str] = field(default_factory=list)
    naira_amount: int | None = None
    resolved_item_id: int | None = None
    resolved_qty: int = 0


@dataclass
class SoldOutNotice:
    item_name: str
    display_name: str
    alternatives: list[tuple[int, str, Decimal]] = field(default_factory=list)


@dataclass
class ParsedOrderDraft:
    matched: list[MatchedOrderItem] = field(default_factory=list)
    followups: list[FollowUpQuestion] = field(default_factory=list)
    please_specify: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    sold_out: list[SoldOutNotice] = field(default_factory=list)
    general_notes: list[str] = field(default_factory=list)
    cart_mode: str = "add"  # "add" | "replace"
    awaiting_cart_mode: bool = False
    followup_index: int = 0
    total_followups: int = 0

    @property
    def has_any_recognition(self) -> bool:
        return bool(self.matched or self.followups or self.sold_out or self.please_specify)

    @property
    def subtotal(self) -> Decimal:
        return sum((m.subtotal for m in self.matched), Decimal("0"))

    def kitchen_notes_summary(self) -> str:
        parts: list[str] = []
        for m in self.matched:
            if m.notes:
                parts.append(f"{m.display_name} ({', '.join(dict.fromkeys(m.notes))})")
        for gn in self.general_notes:
            if gn not in parts:
                parts.append(gn)
        return "; ".join(parts)


def split_order_clauses(text: str) -> list[str]:
    """Split a free-text order into individual item clauses without breaking modifier phrases."""
    cleaned = POLITE_FILLER_RE.sub("", text.strip())
    cleaned = TRAILING_FILLER_RE.sub("", cleaned)
    if not cleaned:
        return []

    # Protect modifier joins like "with stew", "with extra pepper and no sausage" from clause splitting
    protected = re.sub(
        r"\b(with|and)\s+((?:add|extra|more|less|little|no|without|plenty|well)\s+[a-z]+)",
        r"__MODJOIN__ \2",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Protect "1/2 liter" from slash splitting
    protected = re.sub(r"\b1\s*/\s*2\b", "__HALF__", protected)

    raw_parts = re.split(
        r"(?:[,;\n+&]+|\b(?:and|plus|then|also|with)\b)",
        protected,
        flags=re.IGNORECASE,
    )
    clauses: list[str] = []
    for part in raw_parts:
        restored = part.replace("__MODJOIN__", " ").replace("__HALF__", "1/2").strip(" ,.-•*")
        restored = POLITE_FILLER_RE.sub("", restored).strip()
        if restored:
            clauses.append(restored)
    return clauses


def _extract_modifiers(clause: str) -> tuple[str, list[str]]:
    """Extract special request modifiers from a clause and return `(remaining_text, modifiers)`."""
    notes: list[str] = []
    working = clause
    for pattern, canonical_note in SPECIAL_REQUEST_PATTERNS:
        if pattern.search(working):
            if canonical_note not in notes:
                notes.append(canonical_note)
            working = pattern.sub(" ", working)
    # Clean leftover connector words ("with", "but", "no") left dangling after modifier extraction
    working = re.sub(r"\b(?:with|but)\s*$", "", working, flags=re.IGNORECASE).strip(" ,.-()")
    working = re.sub(r"\s{2,}", " ", working).strip()
    return working, notes


def _extract_quantity_and_naira(clause: str) -> tuple[str, int, int | None]:
    """Extract `(item_phrase, quantity, naira_amount)` from a single clause."""
    working = clause.strip()
    naira_amount: int | None = None
    quantity = 1

    # 1. Explicit Naira amount after 'of' / 'worth' / 'for' (e.g. "jollof of 800", "jollof rice worth ₦1,200")
    of_match = re.search(
        r"\b(?:of|worth|for)\s*(?:₦|naira|ngn|#)?\s*(\d[\d,]*)\s*(?:naira|ngn)?\b",
        working,
        re.IGNORECASE,
    )
    if of_match:
        amt_str = of_match.group(1).replace(",", "")
        if amt_str.isdecimal():
            amt = int(amt_str)
            if amt >= 100:
                naira_amount = amt
                working = (working[:of_match.start()] + " " + working[of_match.end():]).strip()

    # 2. Leading Naira prefix (e.g. "₦800 jollof rice" or "#800 jollof")
    if naira_amount is None:
        lead_naira = re.match(r"^(?:₦|#)\s*(\d[\d,]*)\s+(.+)$", working)
        if lead_naira:
            amt_str = lead_naira.group(1).replace(",", "")
            if amt_str.isdecimal() and int(amt_str) >= 100:
                naira_amount = int(amt_str)
                working = lead_naira.group(2).strip()

    # 3. Multiplier syntax: "2x jollof" or "jollof x2" or "jollof x 3"
    mult_lead = re.match(r"^(\d+)\s*x\b\s*(.+)$", working, re.IGNORECASE)
    if mult_lead:
        quantity = max(1, int(mult_lead.group(1)))
        working = mult_lead.group(2).strip()
    else:
        mult_trail = re.search(r"\bx\s*(\d+)$", working, re.IGNORECASE)
        if mult_trail:
            quantity = max(1, int(mult_trail.group(1)))
            working = working[:mult_trail.start()].strip()
        else:
            # 4. Leading digit or number word (unless "1/2 liter" or "5 alive")
            if not re.match(r"^(?:1/2|5\s*alive)\b", working, re.IGNORECASE):
                lead_num = re.match(r"^(\d+|[a-zA-Z]+)\b\s*(.*)$", working)
                if lead_num:
                    token = lead_num.group(1).lower()
                    rest = lead_num.group(2).strip()
                    if rest:
                        if token.isdecimal() and int(token) < 100:
                            quantity = max(1, int(token))
                            working = rest
                        elif token in NUMBER_WORDS:
                            quantity = NUMBER_WORDS[token]
                            working = rest

    # 5. Strip leading unit words ("2 plates of jollof rice" -> "jollof rice")
    tokens = working.split()
    if len(tokens) >= 2 and tokens[0].lower() in UNIT_WORDS:
        tokens = tokens[1:]
        if len(tokens) >= 2 and tokens[0].lower() == "of":
            tokens = tokens[1:]
        working = " ".join(tokens)

    working = re.sub(r"\s{2,}", " ", working).strip(" ,.-")
    return working, quantity, naira_amount


def _normalize_phrase(phrase: str) -> str:
    cleaned = phrase.lower().strip()
    cleaned = re.sub(r"[()\[\]{}'\".,!?;:]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _add_or_merge_matched(
    draft: ParsedOrderDraft,
    item: MenuItem,
    quantity: int,
    notes: list[str],
) -> None:
    """Add a matched item into `draft.matched` (checking availability and stock limits)."""
    display_name = menu_service.friendly_item_name(item.name)
    limit = item.sellable_units
    if not item.is_available or (limit is not None and limit <= 0):
        draft.sold_out.append(SoldOutNotice(item_name=item.name, display_name=display_name))
        return

    for existing in draft.matched:
        if existing.item_id == item.id:
            new_qty = existing.quantity + quantity
            if limit is not None and new_qty > limit:
                existing.stock_capped_from = new_qty
                existing.quantity = limit
            else:
                existing.quantity = new_qty
            for n in notes:
                if n not in existing.notes:
                    existing.notes.append(n)
            return

    capped_from: int | None = None
    effective_qty = quantity
    if limit is not None and quantity > limit:
        capped_from = quantity
        effective_qty = limit

    draft.matched.append(
        MatchedOrderItem(
            item_id=item.id,
            name=item.name,
            display_name=display_name,
            unit_price=item.price,
            quantity=effective_qty,
            notes=list(notes),
            stock_capped_from=capped_from,
        )
    )


def resolve_followup_choice(
    draft: ParsedOrderDraft,
    q: FollowUpQuestion,
    item: MenuItem,
    quantity: int,
) -> None:
    """Apply a customer's follow-up poll selection to `draft` and record it on `q` for `⬅ Go back`."""
    before_qty = next((m.quantity for m in draft.matched if m.item_id == item.id), 0)
    _add_or_merge_matched(draft, item, quantity, q.notes)
    after_qty = next((m.quantity for m in draft.matched if m.item_id == item.id), 0)
    added_delta = max(0, after_qty - before_qty)
    q.resolved_item_id = item.id if added_delta > 0 else None
    q.resolved_qty = added_delta


def undo_followup_choice(draft: ParsedOrderDraft, q: FollowUpQuestion) -> None:
    """Undo a previously applied follow-up poll selection when the customer taps `⬅ Go back`."""
    if q.resolved_item_id is not None and q.resolved_qty > 0:
        for idx, m in enumerate(draft.matched):
            if m.item_id == q.resolved_item_id:
                if m.quantity <= q.resolved_qty:
                    draft.matched.pop(idx)
                else:
                    m.quantity -= q.resolved_qty
                break
    q.resolved_item_id = None
    q.resolved_qty = 0


def parse_typed_order(text: str, catalog: Sequence[MenuItem]) -> ParsedOrderDraft:
    """Parse a customer's free-text message against the live `MenuItem` catalog."""
    draft = ParsedOrderDraft()
    if not text or not text.strip():
        return draft

    by_name: dict[str, MenuItem] = {item.name: item for item in catalog}
    by_lower: dict[str, MenuItem] = {item.name.lower(): item for item in catalog}

    # Build fuzzy candidate dictionary: candidate_phrase -> canonical DB item name
    candidate_to_canonical: dict[str, str] = {}
    for item in catalog:
        candidate_to_canonical[_normalize_phrase(item.name)] = item.name
        friendly = menu_service.friendly_item_name(item.name)
        candidate_to_canonical[_normalize_phrase(friendly)] = item.name
    for alias, target_name in ITEM_ALIASES.items():
        if target_name in by_name:
            candidate_to_canonical[_normalize_phrase(alias)] = target_name

    candidate_keys = list(candidate_to_canonical.keys())

    clauses = split_order_clauses(text)
    size_followups: list[FollowUpQuestion] = []
    fuzzy_followups: list[FollowUpQuestion] = []

    for clause in clauses:
        without_mods, notes = _extract_modifiers(clause)
        if not without_mods and notes:
            # Modifier-only clause: attach to the most recently matched item if present
            if draft.matched:
                for n in notes:
                    if n not in draft.matched[-1].notes:
                        draft.matched[-1].notes.append(n)
            else:
                draft.general_notes.extend(n for n in notes if n not in draft.general_notes)
            continue

        item_phrase, qty, naira_amt = _extract_quantity_and_naira(without_mods)
        norm = _normalize_phrase(item_phrase)
        if not norm:
            continue

        # Check if `norm + " " + str(naira_amt)` matches a size-by-price alias (e.g. "shawarma of 1200" or "shawarma 2000")
        if naira_amt is not None:
            price_alias_norm = f"{norm} {naira_amt}"
            if price_alias_norm in ITEM_ALIASES and ITEM_ALIASES[price_alias_norm] in by_name:
                matched_item = by_name[ITEM_ALIASES[price_alias_norm]]
                _add_or_merge_matched(draft, matched_item, qty, notes)
                continue
            # Also check if naira_amt is a multiple of a size-family member (e.g. "shawarma of 2400" -> 2 x Shawama 1200)
            if norm in SIZE_FAMILIES:
                _, member_names, _ = SIZE_FAMILIES[norm]
                exact_members = [
                    by_name[m] for m in member_names
                    if m in by_name and by_name[m].price > 0 and naira_amt % int(by_name[m].price) == 0
                ]
                if len(exact_members) == 1:
                    chosen = exact_members[0]
                    computed_qty = max(1, naira_amt // int(chosen.price))
                    _add_or_merge_matched(draft, chosen, computed_qty, notes)
                    continue

        # 1. Exact alias or exact item name match
        canonical_name = candidate_to_canonical.get(norm)
        if canonical_name and canonical_name in by_name:
            item = by_name[canonical_name]
            if naira_amt is not None and item.price > 0:
                unit_int = int(item.price)
                if naira_amt % unit_int == 0 and naira_amt >= unit_int:
                    qty = naira_amt // unit_int
                else:
                    low_q = max(1, naira_amt // unit_int)
                    high_q = low_q + 1
                    display = menu_service.friendly_item_name(item.name)
                    size_followups.append(
                        FollowUpQuestion(
                            kind="naira_mismatch",
                            raw_phrase=clause,
                            quantity=low_q,
                            family_label=f"{display} ({naira(item.price)} per portion)",
                            options=[
                                (item.id, f"{low_q} portion{'s' if low_q != 1 else ''} of {display}", item.price * low_q),
                                (item.id, f"{high_q} portions of {display}", item.price * high_q),
                            ],
                            example_hint=f"{low_q} {display.lower()}",
                            notes=notes,
                            naira_amount=naira_amt,
                        )
                    )
                    continue
            _add_or_merge_matched(draft, item, qty, notes)
            continue

        # 2. Ambiguous size family (e.g. bare "chicken", "turkey", "shawarma", "fish", "popcorn", "ice cream", "malt")
        if norm in SIZE_FAMILIES:
            family_label, member_names, example_hint = SIZE_FAMILIES[norm]
            options: list[tuple[int, str, Decimal]] = []
            for m_name in member_names:
                m_item = by_name.get(m_name)
                if m_item is not None and m_item.is_available and (m_item.sellable_units is None or m_item.sellable_units > 0):
                    options.append((m_item.id, menu_service.friendly_item_name(m_item.name), m_item.price))
            if options:
                size_followups.append(
                    FollowUpQuestion(
                        kind="size",
                        raw_phrase=item_phrase,
                        quantity=qty,
                        family_label=family_label,
                        options=options,
                        example_hint=example_hint,
                        notes=notes,
                    )
                )
            else:
                draft.sold_out.append(SoldOutNotice(item_name=family_label, display_name=family_label))
            continue

        # 3. Fuzzy match against size families first (e.g. "chiken", "tukey", "sharwarma")
        best_family_key = None
        best_family_score = 0.0
        for fam_key in SIZE_FAMILIES:
            score = float(fuzz.token_sort_ratio(norm, fam_key))
            if score > best_family_score:
                best_family_score = score
                best_family_key = fam_key

        # 4. Fuzzy match against specific menu items & aliases
        best_cand_key = None
        best_cand_score = 0.0
        if rf_process is not None and candidate_keys:
            extracted = rf_process.extractOne(norm, candidate_keys, scorer=fuzz.token_sort_ratio)
            if extracted:
                best_cand_key, best_cand_score = extracted[0], float(extracted[1])
        else:
            for ck in candidate_keys:
                sc = float(fuzz.token_sort_ratio(norm, ck))
                if sc > best_cand_score:
                    best_cand_score = sc
                    best_cand_key = ck

        # If it strongly matches a bare size family (like "chiken" -> "chicken") better than a specific sized item
        if best_family_key and best_family_score >= 84.0 and best_family_score >= best_cand_score - 2.0:
            family_label, member_names, example_hint = SIZE_FAMILIES[best_family_key]
            options = [
                (by_name[m].id, menu_service.friendly_item_name(by_name[m].name), by_name[m].price)
                for m in member_names
                if m in by_name and by_name[m].is_available and (by_name[m].sellable_units is None or by_name[m].sellable_units > 0)
            ]
            if options:
                size_followups.append(
                    FollowUpQuestion(
                        kind="size",
                        raw_phrase=item_phrase,
                        quantity=qty,
                        family_label=family_label,
                        options=options,
                        example_hint=example_hint,
                        notes=notes,
                    )
                )
                continue

        if best_cand_key and best_cand_score >= HIGH_CONFIDENCE_SCORE:
            item = by_name[candidate_to_canonical[best_cand_key]]
            if naira_amt is not None and item.price > 0 and naira_amt >= int(item.price):
                unit_int = int(item.price)
                if naira_amt % unit_int == 0:
                    qty = naira_amt // unit_int
            _add_or_merge_matched(draft, item, qty, notes)
            continue

        if best_cand_key and best_cand_score >= MEDIUM_CONFIDENCE_SCORE:
            item = by_name[candidate_to_canonical[best_cand_key]]
            display = menu_service.friendly_item_name(item.name)
            fuzzy_followups.append(
                FollowUpQuestion(
                    kind="fuzzy",
                    raw_phrase=item_phrase,
                    quantity=qty,
                    family_label=display,
                    options=[(item.id, display, item.price)],
                    example_hint=f"{qty} {display.lower()}",
                    notes=notes,
                )
            )
            continue

        logger.info("Unmatched typed order phrase: %r (best=%r at %.1f)", item_phrase, best_cand_key, best_cand_score)
        draft.unmatched.append(item_phrase)

    # Populate in-stock category alternatives for any sold-out items (up to 2 per sold-out item, sorted by popularity)
    matched_ids = {m.item_id for m in draft.matched}
    for notice in draft.sold_out:
        orig = by_name.get(notice.item_name) or by_lower.get(notice.item_name.lower())
        if orig is not None:
            pop_list = menu_service.ITEM_POPULARITY_ORDER.get(orig.category, [])
            rank = {name: idx for idx, name in enumerate(pop_list)}
            cand_items = sorted(
                (
                    m
                    for m in catalog
                    if m.category == orig.category
                    and m.id != orig.id
                    and m.id not in matched_ids
                    and m.is_available
                    and (m.sellable_units is None or m.sellable_units > 0)
                ),
                key=lambda m: (rank.get(m.name, 999), m.name.lower()),
            )[:2]
            notice.alternatives = [
                (m.id, menu_service.friendly_item_name(m.name), m.price) for m in cand_items
            ]

    # Prioritize follow-ups: size/naira questions first, then fuzzy "Did you mean?" checks (max 2 polls)
    all_followups = size_followups + fuzzy_followups
    draft.followups = all_followups[:MAX_FOLLOWUP_POLLS]
    draft.total_followups = len(draft.followups)
    for overflow_q in all_followups[MAX_FOLLOWUP_POLLS:]:
        draft.please_specify.append(f"{overflow_q.raw_phrase} → type *{overflow_q.example_hint}*")

    return draft
