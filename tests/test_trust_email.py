"""Parser checks against real Trust/Grab/PayLah email shapes.

Bodies mirror the real senders' HTML exactly (whitespace runs, entities,
field order) but every name, account digit, booking code and location is
fabricated."""
from datetime import datetime

from app.trust_email import (parse_trust_email, is_spend_email, classify_email,
                             parse_grab_email, parse_paylah_email,
                             parse_fairprice_email, parse_shopback_email,
                             parse_dbs_email, suggest_receipt,
                             TRUST_SENDER, GRAB_SENDER, PAYLAH_SENDER,
                             FAIRPRICE_SENDER, SHOPBACK_SENDER, DBS_SENDER)

# Real bodies: note the runs of whitespace inside merchant names and the &#39;
# entity, both of which the parser must normalise away.
LOCAL = ('<p><span>Didn&#39;t do this? You&#39;ve spent SGD 3.39 at '
         'BUS/MRT                  SINGAPORE    SG on 9 Jul 2026 06:24SGT '
         'with Trust Link card. Not you?<br></span></p>')
OVERSEAS = ('<span>0% FX fees! You&#39;ve spent SGD 82.69 at '
            'Steam Purchase           425-952-2985 DE with Trust Link card on '
            '6 Jul 2026 22:11SGT. Not you?</span>')
PAYNOW = ('<span>Your PayNow transfer of SGD 7.00 from A/C ending 5678 to '
          'JOHXX DOX TAX on 09 Jul 2026 14:56 SGT is successful.</span>')
STATEMENT = '<span>Your monthly Stocks account statement is ready! Log into Trust App.</span>'


def test_local_card():
    r = parse_trust_email("Yay! Transaction successful", LOCAL)
    assert r == {"amount_cents": 339, "note": "BUS/MRT SINGAPORE SG",
                 "created_at": "2026-07-09T06:24:00", "kind": "card"}


def test_overseas_card_word_order_flips():
    r = parse_trust_email("Yay! Overseas transaction successful", OVERSEAS)
    assert r == {"amount_cents": 8269, "note": "Steam Purchase 425-952-2985 DE",
                 "created_at": "2026-07-06T22:11:00", "kind": "card"}


def test_paynow_out():
    r = parse_trust_email("Paynow transfer", PAYNOW)
    assert r == {"amount_cents": 700, "note": "JOHXX DOX TAX",
                 "created_at": "2026-07-09T14:56:00", "kind": "paynow"}


def test_statement_is_not_a_transaction():
    assert parse_trust_email("statement ready", STATEMENT) is None
    assert is_spend_email(STATEMENT) is False


def test_spend_emails_are_flagged_for_catch_all():
    assert is_spend_email(LOCAL) and is_spend_email(OVERSEAS) and is_spend_email(PAYNOW)


# ---- Grab e-receipts ------------------------------------------------------

NOW = datetime(2026, 7, 10, 20, 0, 0)

GRAB_FOOD = ('<td>Hope you enjoyed your food!</td><td>TOTAL</td>'
             '<td>SGD 1.40</td><td>DATE | TIME</td><td>10 Jul 26 12:18 +0800</td>'
             '<td>Order Details</td><td>Vehicle type: GrabFood</td>'
             '<td>Issued to Alex</td><td>Booking code 0012345678-ABCDEFGHIJKLMN</td>'
             '<td>Order from: Coke&amp;Go Cooler 123456789 - Example Mall '
             'Level 1 Lobby Profile: Personal</td>')
GRAB_RIDE = ('<div>Standard | Car or taxi</div><div>Hope you enjoyed your ride!</div>'
             '<div>Picked up on 12 June 2026</div><div>Booking ID: A-1AAAABBBBCCCCD</div>'
             '<td>Total Paid</td><td><div>SGD 17.70</div></td>')
GRAB_MARKETING = '<div>Temporary fuel surcharge will be maintained until 31 July 2026</div>'


def test_grab_food_order():
    r = parse_grab_email("Your Grab E-Receipt", GRAB_FOOD, NOW)
    assert r == {"amount_cents": 140, "note": "GrabFood Coke&Go Cooler",
                 "created_at": "2026-07-10T12:18:00", "kind": "grab"}


def test_grab_ride_uses_email_time():
    r = parse_grab_email("Your Grab E-Receipt", GRAB_RIDE, NOW)
    assert r == {"amount_cents": 1770, "note": "Grab ride",
                 "created_at": "2026-07-10T20:00:00", "kind": "grab"}


def test_grab_marketing_is_skipped():
    assert parse_grab_email("Adjustments to Fuel Surcharge", GRAB_MARKETING, NOW) is None
    assert classify_email(GRAB_SENDER, "Adjustments to Fuel Surcharge",
                          GRAB_MARKETING, NOW) == ("skip", None)


# ---- PayLah! alerts (Scan & Pay / Transfer / Bill Payment share one table) --

def _paylah(kind_phrase, to_cell):
    return (f'<td>We refer to your PayLah! {kind_phrase} dated 03 Jul. We are '
            'pleased to confirm that the transaction was completed.</td>'
            '<tr><td>Date &amp; Time:</td><td>03 Jul 13:40 (SGT)</td></tr>'
            '<tr><td>Amount:</td><td>SGD1.60</td></tr>'
            '<tr><td>From:</td><td>PayLah! Wallet (Mobile ending 1111)</td></tr>'
            f'<tr><td>To:</td><td>{to_cell}</td></tr>'
            '<td>To view your transactions, login to your PayLah! Wallet</td>')


def test_paylah_scan_and_pay():
    r = parse_paylah_email("Transaction Alerts",
                           _paylah("Scan & Pay Transfer", "KOPITIAM KAYA TOAST"), NOW)
    assert r == {"amount_cents": 160, "note": "KOPITIAM KAYA TOAST",
                 "created_at": "2026-07-03T13:40:00", "kind": "paylah"}


def test_paylah_transfer_strips_mobile_suffix():
    r = parse_paylah_email("Transaction Alerts",
                           _paylah("Transfer", "Jane Doe (Mobile ending 2222)"), NOW)
    assert r["note"] == "Jane Doe"


def test_paylah_year_rollover():
    # Alert sent early Jan for a late-Dec transaction: no year in the body.
    body = _paylah("Transfer", "Jane Doe").replace("03 Jul", "30 Dec")
    r = parse_paylah_email("Transaction Alerts", body, datetime(2027, 1, 2, 9, 0))
    assert r["created_at"] == "2026-12-30T13:40:00"


def test_paylah_incoming_money_is_skipped():
    body = _paylah("Transfer", "Jane Doe").replace(
        "From:</td><td>PayLah! Wallet (Mobile ending 1111)",
        "From:</td><td>Jane Doe (Mobile ending 2222)")
    assert parse_paylah_email("Transaction Alerts", body, NOW) is None
    assert classify_email(PAYLAH_SENDER, "Transaction Alerts", body, NOW) == ("skip", None)


# ---- classify: dedup + cancellation ----------------------------------------

GRAB_VIA_TRUST = ('<span>You&#39;ve spent SGD 17.70 at Grab* A-1AAAABBBBCCCCD '
                  'Singapore SG on 12 Jun 2026 19:33SGT with Trust Link card.</span>')
TRUST_CANCELLED = ('<span>We&#39;ve cancelled your purchase of SGD 18.50 at '
                   'Grab* A-2EEEEFFFFGGGGH Singapore SG on 12 Jun 2026 22:20SGT '
                   'with Trust Link card.</span>')


def test_trust_grab_spend_is_deduped():
    # The Grab e-receipt is the authoritative record for Grab spends.
    assert classify_email(TRUST_SENDER, "Yay! Transaction successful 😁",
                          GRAB_VIA_TRUST, NOW) == ("skip", None)


def test_trust_grab_cancellation_is_silent():
    assert classify_email(TRUST_SENDER, "Transaction cancelled 💳",
                          TRUST_CANCELLED, NOW) == ("skip", None)


def test_trust_non_grab_cancellation_warns():
    body = TRUST_CANCELLED.replace("Grab* A-2EEEEFFFFGGGGH Singapore SG", "SHOPEE SG")
    action, info = classify_email(TRUST_SENDER, "Transaction cancelled 💳", body, NOW)
    assert action == "cancelled"
    assert info == {"amount_cents": 1850, "note": "SHOPEE SG"}


def test_trust_normal_spend_still_imports():
    action, parsed = classify_email(TRUST_SENDER, "Yay!", LOCAL, NOW)
    assert action == "import" and parsed["amount_cents"] == 339


# ---- FairPrice Group app receipts ------------------------------------------

FAIRPRICE = ('<td>Your payment is complete</td><td>Hi Alex,</td>'
             '<td>This is your summary for <b>Payment Ref. C-12345678</b>.</td>'
             '<td>Summary</td><td>Total</td><td>$6.70</td>'
             '<td>Paid By</td><td>$6.70</td><td>Apple Pay (**** 1234)</td>'
             '<td>Total savings</td><td>$0.27</td>')


def test_fairprice_app_receipt():
    r = parse_fairprice_email("Your FairPrice Group app receipt", FAIRPRICE, NOW)
    assert r == {"amount_cents": 670, "note": "FairPrice",
                 "created_at": "2026-07-10T20:00:00", "kind": "fairprice"}


def test_fairprice_app_trust_alert_is_deduped():
    # App payments hit the Trust card too, with a distinctive merchant string
    # ("...App Paymen" runs into the city field in the real alert).
    body = LOCAL.replace("BUS/MRT                  SINGAPORE    SG",
                         "NTUC FairPrice App PaymenSINGAPORE SG")
    assert classify_email(TRUST_SENDER, "Yay!", body, NOW) == ("skip", None)


def test_fairprice_till_tap_still_imports():
    # A direct card tap at a FairPrice till has no "App Paymen" marker.
    body = LOCAL.replace("BUS/MRT                  SINGAPORE    SG",
                         "NTUC FP-ORCHARD SINGAPORE SG")
    action, parsed = classify_email(TRUST_SENDER, "Yay!", body, NOW)
    assert action == "import" and parsed["note"] == "NTUC FP-ORCHARD SINGAPORE SG"


# ---- ShopBack Pay receipts ---------------------------------------------------

SHOPBACK = ('<td>Thanks for shopping smarter with ShopBack.<br><br>Here’s the '
            'receipt for the payment you made at CHICHA San Chen - 吃茶三千 '
            '(Orchard) using ShopBack Pay.</td>'
            '<td><b>Order ID</b> 260627-3952-UOOL<br>'
            '<b>Payment date</b> 27 Jun 2026, 8:38 PM</td>'
            '<td>Subtotal (incl. GST)</td><td>$10.80</td>'
            '<td>Total paid</td><td>$10.80</td>')


def test_shopback_pay_receipt():
    r = parse_shopback_email("Your ShopBack Pay receipt - $10.80", SHOPBACK, NOW)
    assert r == {"amount_cents": 1080, "note": "CHICHA San Chen - 吃茶三千 (Orchard)",
                 "created_at": "2026-06-27T20:38:00", "kind": "shopback"}


def test_shopback_cashback_notice_is_skipped():
    body = "<td>Cashback confirmed $33.93 is officially yours! From Agoda</td>"
    assert classify_email(SHOPBACK_SENDER, "Your Cashback is confirmed",
                          body, NOW) == ("skip", None)


def test_shopback_trust_alert_is_deduped():
    body = LOCAL.replace("BUS/MRT                  SINGAPORE    SG",
                         "ShopBack CHICHA San Chen ShopBack SG")
    assert classify_email(TRUST_SENDER, "Yay!", body, NOW) == ("skip", None)


# ---- DBS iBanking PayNow (bank account, not PayLah) --------------------------

DBS_PAYNOW = ('<p>Dear Customer,<br><br>We refer to your PAYNOW dated 12 Jun. '
              'We are pleased to confirm that the transaction was completed.<br>'
              '<table><tr><td>Date & Time:</td><td>12 Jun 19:19 (SGT)</td></tr>'
              '<tr><td>Amount:</td><td>SGD160.00</td></tr>'
              '<tr><td>From:</td><td>Alex Tan (alexdoe) A/C ending 1234</td></tr>'
              '<tr><td>To:</td><td>ACME GADGETS PTE. LTD. (UEN ending 123A)</td></tr>'
              '</table><br>If unauthorised, please call our DBS hotline. '
              'To view transaction details, please login to digibank.</p>')


def test_dbs_paynow_outgoing():
    r = parse_dbs_email("iBanking Alerts", DBS_PAYNOW, NOW)
    assert r == {"amount_cents": 16000, "note": "ACME GADGETS PTE. LTD.",
                 "created_at": "2026-06-12T19:19:00", "kind": "dbs-paynow"}


def test_dbs_received_transfer_is_skipped():
    body = "<td>digibank Alerts - You've received a transfer</td>"
    assert classify_email(DBS_SENDER, "digibank Alerts - You've received a transfer",
                          body, NOW) == ("skip", None)


# ---- generic catch-all (unknown senders) ------------------------------------

def test_unknown_sender_receipt_is_suggested():
    body = "<td>Thanks for your order!</td><td>Grand Total</td><td>SGD 45.90</td>"
    action, info = classify_email("orders@somecafe.sg", "Your receipt from SomeCafe",
                                  body, NOW)
    assert action == "suggest"
    assert info == {"amount_cents": 4590, "note": "Your receipt from SomeCafe"}


def test_unknown_sender_marketing_is_skipped():
    assert classify_email("hello@secretlab.co", "Up to $247 off this Father's Day",
                          "<td>Save up to $247 on chairs</td>", NOW) == ("skip", None)


def test_receipt_subject_without_amount_is_skipped():
    assert suggest_receipt("a@b.co", "Your receipt", "<td>no numbers here</td>") is None


# ---- incoming-money → suggest-as-income -------------------------------------

def test_incoming_transfer_suggests_income():
    body = ("<p>Dear Sir/Madam, You've received a transfer of SGD250.00 "
            "on 10 Jul 14:00 (SGT) From: JOHN TAN.</p>")
    action, info = classify_email(
        DBS_SENDER, "Transaction Alerts", body, datetime(2026, 7, 10, 14, 5))
    assert action == "suggest_income"
    assert info["amount_cents"] == 25000


def test_incoming_only_from_bank_senders():
    body = "<p>You've received SGD 9,999.00! Claim now.</p>"
    action, _ = classify_email(
        "spam@example.com", "You've received money", body, datetime(2026, 7, 10, 14, 5))
    assert action != "suggest_income"


def test_outgoing_paynow_still_imports():
    # regression guard: the existing DBS outgoing path must win over the income
    # heuristic. Reuses the DBS_PAYNOW fixture (already covered by
    # test_dbs_paynow_outgoing above) rather than inventing a new body.
    action, parsed = classify_email(DBS_SENDER, "iBanking Alerts", DBS_PAYNOW, NOW)
    assert action == "import"
