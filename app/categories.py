from __future__ import annotations
from app.config import DEFAULT_CATEGORY

# (keywords, category) — first rule with any matching substring wins.
# Keywords are matched case-insensitively against the note text.
# Common Singaporean merchant/term shorthand; near-synonyms are pruned.
# NOTE: Food must precede Transport so "grabfood" resolves to Food, not the
# "grab" (ride-hailing) rule below it.
KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    # SG supermarkets (grabmart before the "grab" ride rule, like grabfood)
    (("fairprice", "ntuc", "sheng siong", "giant", "cold storage",
      "donki", "redmart", "grabmart"), "Groceries"),
    # Hawker, kopitiam, fast food, delivery
    (("hawker", "kopitiam", "kopi", "makan", "cai png", "chicken rice",
      "mcdonald", "kfc", "mixue", "koi", "foodpanda", "grabfood",
      "restaurant", "cafe"), "Food"),
    # Ride-hailing, public transit, fuel, parking
    (("grab", "gojek", "tada", "taxi", "mrt", "bus", "ezlink", "simplygo",
      "petrol", "parking", "erp"), "Transport"),
    # Online marketplaces + retail
    (("shopee", "lazada", "taobao", "qoo10", "amazon", "uniqlo",
      "decathlon", "ikea"), "Shopping"),
    # Telco, utilities, subscriptions, insurance, rent
    (("sp group", "singtel", "starhub", "m1", "simba", "netflix", "spotify",
      "disney", "youtube", "insurance", "rent", "hdb", "town council",
      "wifi"), "Bills"),
    # Clinics, pharmacies, gym, salon
    (("clinic", "polyclinic", "dentist", "pharmacy", "guardian", "watsons",
      "unity", "hospital", "gym", "salon", "barber"), "Health"),
    # Flights, stays, booking platforms (day-to-day overseas spend is /trip mode's job)
    (("flight", "hotel", "hostel", "airport", "changi", "airbnb", "agoda",
      "klook", "trip.com", "traveloka", "scoot", "jetstar"), "Travel"),
    # Cinemas, games, concerts
    (("gv", "shaw", "cathay", "cinema", "steam", "concert"), "Entertainment"),
]


def guess_category(note: str) -> str:
    text = note.lower()
    for keywords, category in KEYWORD_RULES:
        if any(kw in text for kw in keywords):
            return category
    return DEFAULT_CATEGORY
