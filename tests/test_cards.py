"""Cards: storage, instrument binding, resolution, and learning."""
import pytest

from app import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    yield c
    c.close()


def test_schema_migration_is_idempotent(conn):
    # init_schema runs on every boot against the live DB.
    db.init_schema(conn)
    db.init_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
    assert "card_id" in cols and "instrument" in cols


def test_existing_transactions_are_unassigned(conn):
    tid = db.add_transaction(conn, 500, "kopi", "Food", "telegram")
    row = db.get_transaction(conn, tid)
    assert row["card_id"] is None
    assert row["instrument"] is None


def test_card_crud(conn):
    cid = db.add_card(conn, "Trust Link", "debit", last4="9012")
    assert db.get_card(conn, cid)["name"] == "Trust Link"
    assert db.update_card(conn, cid, name="Trust Debit") is True
    assert db.get_card(conn, cid)["name"] == "Trust Debit"
    assert [c["id"] for c in db.list_cards(conn)] == [cid]


def test_archiving_hides_a_card_but_keeps_it(conn):
    cid = db.add_card(conn, "Old Card", "debit")
    assert db.archive_card(conn, cid) is True
    assert db.list_cards(conn) == []
    assert [c["id"] for c in db.list_cards(conn, include_archived=True)] == [cid]
    assert db.get_card(conn, cid) is not None


def test_a_referenced_card_is_never_hard_deleted(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    db.add_transaction(conn, 500, "kopi", "Food", "trust-email", card_id=cid)
    assert db.card_tx_count(conn, cid) == 1
    assert db.delete_card(conn, cid) is False
    assert db.get_card(conn, cid) is not None


def test_an_unreferenced_card_can_be_deleted(conn):
    cid = db.add_card(conn, "Typo", "debit")
    assert db.delete_card(conn, cid) is True
    assert db.get_card(conn, cid) is None


def test_instrument_binding_roundtrip(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    db.bind_instrument(conn, "trust-email", "trust link card", cid)
    assert db.recall_instrument(conn, "trust-email", "trust link card") == cid
    assert db.recall_instrument(conn, "trust-email", "other card") is None
    # Scoped by source: the same token under another sender is a different key.
    assert db.recall_instrument(conn, "dbs-email", "trust link card") is None


def test_rebinding_replaces_and_resets_hits(conn):
    a = db.add_card(conn, "A", "debit")
    b = db.add_card(conn, "B", "debit")
    db.bind_instrument(conn, "trust-email", "", a)
    db.recall_instrument(conn, "trust-email", "")
    db.bind_instrument(conn, "trust-email", "", b)
    assert db.recall_instrument(conn, "trust-email", "") == b
    row = conn.execute("SELECT hits FROM card_instruments WHERE source='trust-email'"
                       " AND token=''").fetchone()
    assert row["hits"] == 1  # reset to 0 on rebind, then bumped by the recall above


def test_month_card_spend_buckets_unassigned_separately(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    db.add_transaction(conn, 1000, "a", "Food", "trust-email",
                       created_at="2026-08-02T10:00:00", card_id=cid)
    db.add_transaction(conn, 250, "b", "Food", "telegram",
                       created_at="2026-08-03T10:00:00")
    rows = {r["card_id"]: r["total_cents"] for r in db.month_card_spend(conn, "2026-08")}
    assert rows == {cid: 1000, None: 250}


from app import cards


def test_normalisation_is_lowercase_and_whitespace_only():
    assert cards.normalise_instrument("  Trust   Link  card ") == "trust link card"
    # merchant_stem would mangle these; instruments are machine-generated and
    # must survive intact.
    assert cards.normalise_instrument("A/C ending 1234") == "a/c ending 1234"
    assert cards.normalise_instrument("Apple Pay (**** 4242)") == "apple pay (**** 4242)"
    assert cards.normalise_instrument(None) == ""


def test_unknown_instrument_is_unassigned(conn):
    assert cards.effective_card(conn, "trust-email", "nothing") == (None, None)


def test_source_default_attributes_everything_from_that_sender(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    db.bind_instrument(conn, "trust-email", "", cid)
    assert cards.effective_card(conn, "trust-email", "trust link card") == (cid, "source")
    assert cards.effective_card(conn, "dbs-email", "anything") == (None, None)


def test_a_specific_token_beats_the_source_default(conn):
    debit = db.add_card(conn, "Trust Debit", "debit")
    credit = db.add_card(conn, "Trust Credit", "credit")
    db.bind_instrument(conn, "trust-email", "", debit)
    db.bind_instrument(conn, "trust-email", "trust cashback card", credit)
    assert cards.effective_card(conn, "trust-email", "trust cashback card") == \
        (credit, "memory")
    assert cards.effective_card(conn, "trust-email", "trust link card") == (debit, "source")


def test_typed_rule_matches_as_a_substring_of_the_token(conn):
    cid = db.add_card(conn, "Trust Link", "debit", match="4242")
    assert cards.effective_card(conn, "fairprice-email", "apple pay (**** 4242)") == \
        (cid, "rule")


def test_a_blank_match_rule_never_matches(conn):
    db.add_card(conn, "Blank", "debit", match="   ")
    assert cards.effective_card(conn, "trust-email", "trust link card") == (None, None)


def test_a_correction_beats_a_typed_rule(conn):
    # THE regression this design exists to prevent. Learning only happens on a
    # correction; if the typed rule outranked it, the next charge would snap
    # back to the rule and the correction would appear to do nothing.
    ruled = db.add_card(conn, "Ruled", "debit", match="4242")
    corrected = db.add_card(conn, "Corrected", "credit")
    db.bind_instrument(conn, "fairprice-email", "apple pay (**** 4242)", corrected)
    assert cards.effective_card(conn, "fairprice-email", "apple pay (**** 4242)") == \
        (corrected, "memory")


def test_colliding_match_rules_resolve_lowest_id_first(conn):
    first = db.add_card(conn, "First", "debit", match="4242")
    db.add_card(conn, "Second", "credit", match="4242")
    assert cards.effective_card(conn, "x", "**** 4242")[0] == first


def test_archived_cards_never_resolve(conn):
    cid = db.add_card(conn, "Old", "debit", match="4242")
    db.bind_instrument(conn, "trust-email", "", cid)
    db.archive_card(conn, cid)
    assert cards.effective_card(conn, "trust-email", "**** 4242") == (None, None)


def test_learning_needs_an_instrument(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    tid = db.add_transaction(conn, 500, "kopi", "Food", "telegram")
    assert cards.learn_card_from_correction(conn, db.get_transaction(conn, tid), cid) \
        is False


def test_learning_binds_the_rows_instrument(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    tid = db.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                             instrument="trust link card")
    assert cards.learn_card_from_correction(conn, db.get_transaction(conn, tid), cid) \
        is True
    assert cards.effective_card(conn, "trust-email", "trust link card") == (cid, "memory")


def test_learning_to_unassigned_forgets_the_binding(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    tid = db.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                             instrument="trust link card")
    row = db.get_transaction(conn, tid)
    cards.learn_card_from_correction(conn, row, cid)
    cards.learn_card_from_correction(conn, row, None)
    assert cards.effective_card(conn, "trust-email", "trust link card") == (None, None)


# ---- assign_card: the shared "set and learn" used by web AND bot -------------

def test_assign_card_sets_and_learns(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    tid = db.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                             instrument="trust link card")
    assert cards.assign_card(conn, tid, cid) is True
    assert db.get_transaction(conn, tid)["card_id"] == cid
    assert cards.effective_card(conn, "trust-email", "trust link card") == (cid, "memory")


def test_assigning_the_same_card_teaches_nothing(conn):
    # The pre-update comparison, owned here rather than in each front end. Both
    # the web edit sheet and the bot picker re-send the current card on an
    # unrelated save; without this, every save would re-teach.
    cid = db.add_card(conn, "Trust Link", "debit")
    tid = db.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                             instrument="trust link card", card_id=cid)
    assert cards.assign_card(conn, tid, cid) is True
    assert cards.effective_card(conn, "trust-email", "trust link card") == (None, None)


def test_assign_card_to_none_clears_and_forgets(conn):
    cid = db.add_card(conn, "Trust Link", "debit")
    tid = db.add_transaction(conn, 500, "kopi", "Food", "trust-email",
                             instrument="trust link card")
    cards.assign_card(conn, tid, cid)
    assert cards.assign_card(conn, tid, None) is True
    assert db.get_transaction(conn, tid)["card_id"] is None
    assert cards.effective_card(conn, "trust-email", "trust link card") == (None, None)


def test_assign_card_on_a_missing_transaction(conn):
    assert cards.assign_card(conn, 9999, None) is False
