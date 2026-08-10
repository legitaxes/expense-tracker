# tests/test_merchants.py
import pytest
from app.merchants import merchant_stem


@pytest.mark.parametrize("raw, expected", [
    # Real strings from tests/test_trust_email.py fixtures.
    ("BUS/MRT                  SINGAPORE    SG", "bus/mrt"),
    ("NTUC FP-ORCHARD SINGAPORE SG", "ntuc fp"),
    ("Grab* A-1B2345CDEFGHI Singapore SG", "grab"),
    ("NTUC FairPrice App PaymenSINGAPORE SG", "ntuc fairprice"),
    ("SHOPEE SG", "shopee"),
])
def test_real_merchant_strings(raw, expected):
    assert merchant_stem(raw) == expected


def test_outlets_of_one_brand_share_a_stem():
    a = merchant_stem("NTUC FP-ORCHARD SINGAPORE SG")
    b = merchant_stem("NTUC FP-MARINA BAY SINGAPORE SG")
    assert a == b == "ntuc fp"


def test_reference_codes_are_dropped_so_repeat_charges_match():
    a = merchant_stem("Grab* A-1B2345CDEFGHI Singapore SG")
    b = merchant_stem("Grab* A-2C6789DEFGHIJ Singapore SG")
    assert a == b == "grab"


def test_trailing_store_number_is_kept_not_treated_as_reference_code():
    # A real merchant fused with a store/outlet number ("circlek88",
    # "shell1234") must stay learnable, not be silently dropped like a
    # per-transaction reference code.
    assert merchant_stem("CIRCLEK88 SG") == "circlek88"
    assert merchant_stem("SHELL1234 SG") == "shell1234"


def test_hyphenated_name_is_not_split_into_a_stub():
    # "7" alone would collide with anything else starting in a digit.
    assert merchant_stem("7-ELEVEN SINGAPORE SG") == "7-eleven"


def test_overseas_support_phone_number_is_dropped_not_kept_as_the_stem():
    # Overseas card charges embed the merchant's support phone number
    # ("866-579-7172"), which is not part of the brand name -- unlike
    # "shell1234"/"circlek88" it's a run of digits and hyphens with no
    # letters at all, so it must still be recognised as noise and dropped,
    # not captured as (part of) the stem. The trailing "US" country code is
    # deliberately left in place: it's a real token (not purely numeric), and
    # it's stable and harmless per merchant -- "netflix us" still works
    # correctly as a key.
    assert merchant_stem("NETFLIX 866-579-7172 US") == "netflix us"


def test_short_real_brand_names_are_not_discarded_as_noise():
    # GV (Golden Village, a cinema chain) and M1 (a Singapore telco) are both
    # real, short brand names. A bare 1-2 letter token must never be treated
    # as noise just for being short -- that permanently makes the merchant
    # unlearnable with no way to notice the drop.
    assert merchant_stem("GV GRAND SG") == "gv grand"
    assert merchant_stem("M1 LTD SG") == "m1 ltd"


def test_leading_digit_in_brand_name_is_not_mistaken_for_a_reference_code():
    # "7-eleven88" strips its trailing "88" store number to "7-eleven", which
    # still contains the brand's own leading "7". That digit belongs to the
    # brand's spelling, not to a generated code, and must not be counted as
    # evidence of one -- otherwise a real, common merchant becomes permanently
    # unlearnable.
    assert merchant_stem("7-ELEVEN88 SG") == "7-eleven88"


def test_digits_inside_brand_name_are_not_mistaken_for_a_reference_code():
    # "24HR"/"24H" outlet naming ("KFC24HR", "7-ELEVEN24HR") puts digits
    # between two letter groups -- neither a leading nor a trailing digit
    # run. That shape must still be kept as the brand name, not dropped as
    # a generated reference code.
    assert merchant_stem("KFC24HR SG") == "kfc24hr"
    assert merchant_stem("7-ELEVEN24HR SG") == "7-eleven24hr"


def test_long_real_brand_name_with_24hr_suffix_is_not_mistaken_for_a_code():
    # A length-based reference-code check was tried and defeated by exactly
    # this shape: "mcdonalds24hr" and "coldstorage24hr" are long real brand
    # names with a 24-hour outlet suffix, not generated codes. Only the
    # alternation count (2, well under the 4+ threshold) tells them apart
    # from an actual reference code.
    assert merchant_stem("MCDONALDS24HR SG") == "mcdonalds24hr"
    assert merchant_stem("COLDSTORAGE24HR SG") == "coldstorage24hr"


def test_locale_suffix_stripped_even_when_glued_to_the_name():
    # Trust truncates long merchants and glues the country on with no space.
    assert merchant_stem("NTUC FairPrice App PaymenSINGAPORE SG") == "ntuc fairprice"


def test_leading_singapore_in_a_real_name_is_kept():
    assert merchant_stem("SINGAPORE AIRLINES SG") == "singapore airlines"


@pytest.mark.parametrize("junk", ["", "   ", None, "12345", "ab", "7"])
def test_junk_is_not_worth_remembering(junk):
    assert merchant_stem(junk) is None
