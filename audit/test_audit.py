#!/usr/bin/env python3
"""Synthetic proof that audit.py actually catches self-deception.

WHY: praxis's real simulator doesn't exist yet (no skeleton delivered), so
there is nothing real to audit. This file plays BOTH roles instead: it is a
tiny, deliberately dishonest journal-writer that builds one clean journal and
one journal per invariant with exactly ONE lie injected, then runs audit.py
against each and checks that (a) the clean one passes and (b) each dishonest
one is rejected for the SPECIFIC invariant it violates — not some other one
by accident, and not silently accepted.

Everything here reuses audit.canonical_hash() and audit.Ledger from the
auditor itself, so a valid journal is valid "by construction" against the
exact same arithmetic the auditor will re-run — any mismatch in the "valid"
case is a real bug, not formula drift between generator and checker.
"""

import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import audit


def d(value) -> str:
    """Serialize a Decimal as a fixed-point string (SCHEMA.md: decimal-string, never float)."""
    return format(Decimal(value), "f")


class Builder:
    """Minimal append-only journal writer: assigns seq, chains prev_hash/event_hash
    exactly the way audit.py expects to recompute them."""

    def __init__(self, run_id="synthetic-run"):
        self.run_id = run_id
        self.events = []
        self.seq = 0
        self.prev_hash = audit.GENESIS_HASH

    def add(self, event_type, event_time_ns, received_ts_ns, payload, caused_by=None, recorded_at_ns=None):
        self.seq += 1
        envelope = {
            "schema_version": "0.1",
            "run_id": self.run_id,
            "seq": self.seq,
            "event_id": f"evt-{self.seq:03d}",
            "event_type": event_type,
            "event_time_ns": event_time_ns,
            "received_ts_ns": received_ts_ns,
            "recorded_at_ns": recorded_at_ns if recorded_at_ns is not None else event_time_ns,
            "caused_by": caused_by or [],
            "prev_hash": self.prev_hash,
            "payload": payload,
        }
        envelope["event_hash"] = audit.canonical_hash(envelope)
        self.prev_hash = envelope["event_hash"]
        self.events.append(envelope)
        return envelope["event_id"]

    def refinalize(self, start_index=0, prev_hash=None):
        """Recompute prev_hash/event_hash for events[start_index:] after a mutation
        made upstream of the chain (e.g. bumping seq). Simulates 'a dishonest writer
        produced this chain', as opposed to 'someone tampered with an already-hashed
        chain' (that second case is exercised by NOT calling this — see
        build_hash_chain_break)."""
        running = prev_hash if prev_hash is not None else (
            audit.GENESIS_HASH if start_index == 0 else self.events[start_index - 1]["event_hash"]
        )
        for ev in self.events[start_index:]:
            ev["prev_hash"] = running
            ev.pop("event_hash", None)
            ev["event_hash"] = audit.canonical_hash(ev)
            running = ev["event_hash"]
        self.prev_hash = running

    def write(self, path):
        with open(path, "w", encoding="utf-8") as f:
            for ev in self.events:
                f.write(json.dumps(ev, sort_keys=True) + "\n")


def build_scenario(mutation=None) -> Builder:
    """One decision -> one order -> one fill -> one account_state -> run_finished.
    `mutation` injects exactly one lie; None builds the honest baseline."""
    b = Builder()
    initial_cash = Decimal("10000.00")
    initial_position = Decimal("0")

    b.add("run_started", 0, 0, payload={
        "initial_cash": d(initial_cash), "initial_position": d(initial_position),
        "fee_model": "flat", "slippage_model": "worst-of-book",
        "stale_after_ns": 5_000_000_000, "config_hash": "cfg-abc123", "code_hash": "code-def456",
    })

    q1_id = b.add("market_quote", 100, 100, payload={
        "symbol": "BTCUSDT", "exchange_ts_ns": 95, "received_ts_ns": 100,
        "bid_price": d("49990.00"), "bid_qty": d("1.0"),
        "ask_price": d("50000.00"), "ask_qty": d("1.0"),
        "source": "synthetic", "raw_payload_hash": "raw-hash-q1",
    })
    q1_hash = b.events[-1]["event_hash"]

    observed_through = 100
    if mutation == "future_observed_through":
        observed_through = 10_000  # decision claims a market view further in the future than the decision itself

    d1_id = b.add("decision", 150, 150, caused_by=[q1_id], payload={
        "strategy_id": "buy-and-hold-v0", "strategy_version": "1",
        "decision_time_ns": 150,
        "observed_through_received_ts_ns": observed_through,
        "input_head_hash": q1_hash,
        "action": "buy", "requested_qty": d("0.01"),
        "config_hash": "cfg-abc123", "code_hash": "code-def456", "rng_seed": 42,
    })

    order_business_id = "ord-1"  # business order_id (order_submitted.payload.order_id) -- distinct
    # from the envelope event_id; fill.payload.order_id must reference THIS, not the event_id.
    o1_id = b.add("order_submitted", 200, 200, caused_by=[d1_id], payload={
        "order_id": order_business_id, "decision_id": d1_id, "side": "buy", "order_type": "market",
        "requested_qty": d("0.01"), "submitted_ts_ns": 200,
    })

    q2_id = b.add("market_quote", 250, 250, payload={
        "symbol": "BTCUSDT", "exchange_ts_ns": 245, "received_ts_ns": 250,
        "bid_price": d("49995.00"), "bid_qty": d("1.0"),
        "ask_price": d("50000.00"), "ask_qty": d("1.0"),
        "source": "synthetic", "raw_payload_hash": "raw-hash-q2",
    })

    fill_quote_id = q2_id
    ask_at_fill_quote = Decimal("50000.00")
    filled_qty = Decimal("0.01")
    execution_price = Decimal("50000.50")  # worse than ask=50000.00 -> honest slippage
    fee = Decimal("0.05")
    available_qty = Decimal("1.0")

    if mutation == "bad_execution_price":
        execution_price = Decimal("49999.00")  # BETTER than ask -> beats the market
    if mutation == "bad_filled_qty":
        filled_qty = Decimal("1.5")  # exceeds top-of-book ask_qty=1.0
    if mutation == "fill_before_submit":
        fill_quote_id = q1_id  # q1.received_ts_ns=100 < order.submitted_ts_ns=200

    book_price = ask_at_fill_quote
    slippage_amount = abs(execution_price - book_price)

    ledger = audit.Ledger(initial_cash, initial_position)
    cash_before, position_before = ledger.cash, ledger.position
    cash_delta, position_delta = ledger.apply_fill("buy", filled_qty, execution_price, fee)

    f1_id = b.add("fill", 250, 250, caused_by=[o1_id, fill_quote_id], payload={
        "fill_id": "fill-1", "order_id": order_business_id, "quote_event_id": fill_quote_id,
        "side": "buy", "filled_qty": d(filled_qty), "book_price": d(book_price),
        "slippage_amount": d(slippage_amount), "execution_price": d(execution_price),
        "available_qty": d(available_qty), "fee": d(fee),
        "cash_delta": d(cash_delta), "position_delta": d(position_delta),
    })

    exp_cash_after = ledger.cash
    if mutation == "bad_account_balance":
        exp_cash_after = exp_cash_after + Decimal("100.00")  # pocket 100 that never happened

    b.add("account_state", 260, 260, caused_by=[f1_id], payload={
        "triggered_by": f1_id,
        "cash_before": d(cash_before), "cash_after": d(exp_cash_after),
        "position_before": d(position_before), "position_after": d(ledger.position),
        "avg_entry_price": d(ledger.avg_entry_price), "realized_pnl": d(ledger.realized_pnl),
        "unrealized_pnl": d("0"), "equity": d(exp_cash_after + ledger.position * execution_price),
        "state_before_hash": "state-hash-before-1", "state_after_hash": "state-hash-after-1",
    })

    as1_hash = b.events[-1]["event_hash"]
    b.add("run_finished", 300, 300, caused_by=[b.events[-1]["event_id"]], payload={
        "final_state_hash": "state-hash-after-1",
        "event_count": len(b.events) + 1,
        "journal_head_hash": as1_hash,
    })
    return b


def build_hash_chain_break() -> Builder:
    """Simulates a rewritten history: the fill's fee is edited AFTER the chain was
    built, but its event_hash is left stale (untouched). audit.py must catch this
    at the fill event itself, before trusting anything past it."""
    b = build_scenario(None)
    fill_event = next(e for e in b.events if e["event_type"] == "fill")
    fill_event["payload"]["fee"] = d("0.00")  # try to quietly erase the fee
    return b


def build_seq_gap() -> Builder:
    """Simulates a dropped/skipped seq counter: bump seq of account_state onward by
    +1 (re-chaining hashes correctly around the new seq values), leaving a gap at
    the seq number that never appears. Hash chain stays internally consistent —
    only the seq counter is wrong — to isolate this from the hash-chain-break case."""
    b = build_scenario(None)
    idx = next(i for i, e in enumerate(b.events) if e["event_type"] == "account_state")
    for ev in b.events[idx:]:
        ev["seq"] += 1
    b.refinalize(start_index=idx, prev_hash=b.events[idx - 1]["event_hash"])
    return b


SCENARIOS = {
    "valid": lambda: build_scenario(None),
    "SS3 bad_execution_price (buy filled at/below ask)": lambda: build_scenario("bad_execution_price"),
    "SS4 bad_filled_qty (exceeds book depth)": lambda: build_scenario("bad_filled_qty"),
    "SS2 fill_before_submit (quote predates order)": lambda: build_scenario("fill_before_submit"),
    "SS6 bad_account_balance (cash_after doesn't reconcile)": lambda: build_scenario("bad_account_balance"),
    "SS1 future_observed_through (decision sees the future)": lambda: build_scenario("future_observed_through"),
    "SS5 hash_chain_break (payload edited post-hoc)": build_hash_chain_break,
    "SS5 seq_gap (seq counter skips a value)": build_seq_gap,
}

EXPECTED_INVARIANT = {
    "valid": None,
    "SS3 bad_execution_price (buy filled at/below ask)": audit.INV_3,
    "SS4 bad_filled_qty (exceeds book depth)": audit.INV_4,
    "SS2 fill_before_submit (quote predates order)": audit.INV_2,
    "SS6 bad_account_balance (cash_after doesn't reconcile)": audit.INV_6,
    "SS1 future_observed_through (decision sees the future)": audit.INV_1,
    "SS5 hash_chain_break (payload edited post-hoc)": audit.INV_5,
    "SS5 seq_gap (seq counter skips a value)": audit.INV_5,
}


def run():
    tmpdir = Path(tempfile.mkdtemp(prefix="papertrade-audit-test-"))
    results = []
    for name, builder_fn in SCENARIOS.items():
        b = builder_fn()
        path = tmpdir / (name.split()[0] + "_" + str(abs(hash(name)) % 10_000) + ".jsonl")
        b.write(path)
        expected = EXPECTED_INVARIANT[name]

        try:
            n = audit.audit(str(path))
            if expected is None:
                ok, outcome, detail = True, "PASS", f"clean, rc=0, {n} events"
            else:
                ok, outcome, detail = False, "FAIL", f"expected {expected} violation but audit accepted the journal"
        except audit.AuditViolation as v:
            if expected is None:
                ok, outcome, detail = False, "FAIL", f"valid journal was rejected: {v}"
            elif v.invariant == expected:
                ok, outcome, detail = True, "PASS", f"caught as [{v.invariant}]: {v.message}"
            else:
                ok, outcome, detail = False, "FAIL", f"expected {expected}, got [{v.invariant}]: {v.message}"

        results.append((name, ok, outcome, detail))
    return results


def main():
    results = run()

    name_w = max(len(r[0]) for r in results)
    print(f"{'scenario':{name_w}}  {'result':6}  detail")
    print("-" * (name_w + 80))
    all_ok = True
    for name, ok, outcome, detail in results:
        all_ok = all_ok and ok
        print(f"{name:{name_w}}  {outcome:6}  {detail}")

    print()
    if all_ok:
        print(f"ALL {len(results)} SCENARIOS BEHAVED AS EXPECTED")
        sys.exit(0)
    else:
        n_fail = sum(1 for r in results if not r[1])
        print(f"{n_fail}/{len(results)} SCENARIOS DID NOT BEHAVE AS EXPECTED")
        sys.exit(1)


if __name__ == "__main__":
    main()
