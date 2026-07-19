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

    def __init__(self, run_id="synthetic-run", schema_version="0.1"):
        self.run_id = run_id
        self.schema_version = schema_version
        self.events = []
        self.seq = 0
        self.prev_hash = audit.GENESIS_HASH

    def add(self, event_type, event_time_ns, received_ts_ns, payload, caused_by=None,
             recorded_at_ns=None, logical_recorded_at_ns=None):
        """`recorded_at_ns` (actual wall time) and `logical_recorded_at_ns`
        (deterministic replay time, SCHEMA.md §7 v2 / DR-004) both default to
        event_time_ns when omitted -- fine for SS1-6 fixtures that never look
        at either. The DR-004 replay fixtures below (build_empty_run) pass
        them explicitly and DIFFERENTLY on purpose: that's exactly the case
        normalized_projection() has to get right (exclude the former, keep
        the latter)."""
        self.seq += 1
        envelope = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "seq": self.seq,
            "event_id": f"evt-{self.seq:03d}",
            "event_type": event_type,
            "event_time_ns": event_time_ns,
            "received_ts_ns": received_ts_ns,
            "recorded_at_ns": recorded_at_ns if recorded_at_ns is not None else event_time_ns,
            "logical_recorded_at_ns": logical_recorded_at_ns if logical_recorded_at_ns is not None else event_time_ns,
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


def build_scenario(mutation=None, run_id=None, fill_execution_price=None, requested_qty=None) -> Builder:
    """One decision -> one order -> one fill -> one account_state -> run_finished.
    `mutation` injects exactly one lie; None builds the honest baseline.

    `run_id`, `fill_execution_price`, `requested_qty` are for the SS7 (replay
    determinism) tests below, NOT for the SS1-6 dishonesty scenarios above:
    they let a caller build a second, still fully SS1-6-valid journal that
    varies exactly one thing (the run's identity, a derived execution price,
    or an input quantity) so replay_compare() has something clean to compare
    against. Unlike `mutation`, none of these three make the journal lie
    about itself — they change what "the honest truth" for this journal is.
    """
    b = Builder(run_id=run_id) if run_id is not None else Builder()
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

    qty = requested_qty if requested_qty is not None else "0.01"

    d1_id = b.add("decision", 150, 150, caused_by=[q1_id], payload={
        "strategy_id": "buy-and-hold-v0", "strategy_version": "1",
        "decision_time_ns": 150,
        "observed_through_received_ts_ns": observed_through,
        "input_head_hash": q1_hash,
        "action": "buy", "requested_qty": d(qty),
        "config_hash": "cfg-abc123", "code_hash": "code-def456", "rng_seed": 42,
    })

    order_business_id = "ord-1"  # business order_id (order_submitted.payload.order_id) -- distinct
    # from the envelope event_id; fill.payload.order_id must reference THIS, not the event_id.
    o1_id = b.add("order_submitted", 200, 200, caused_by=[d1_id], payload={
        "order_id": order_business_id, "decision_id": d1_id, "side": "buy", "order_type": "market",
        "requested_qty": d(qty), "submitted_ts_ns": 200,
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
    if fill_execution_price is not None:
        # SS7 nondeterminism fixture: still honest (worse-than-ask, fee/cash/position
        # all recomputed below from THIS price via the same Ledger the auditor uses) —
        # just a different honest outcome, standing in for "two runs of the same
        # deterministic simulator disagreed on the execution price".
        execution_price = Decimal(fill_execution_price)

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


def build_empty_run(
    run_id=None,
    replay_group_id="rg-1",
    schema_version="0.2",
    rng_seed=42,
    config_hash="cfg-abc123",
    code_hash="code-def456",
    dataset_hash="ds-empty-v1",
    wall_clock_offset_ns=0,
    mutation=None,
    normalized_final_state_hash="norm-final-empty",
    final_state_hash="state-final-1",
) -> Builder:
    """run_started -> input_attached (empty dataset) -> run_finished. The
    minimal §7 v0 fixture from DR-004's own "первый прогон" plan: pure input/
    output plumbing, no market data or fills, so it isolates the DR-004
    normalization machinery (recorded_at_ns exclusion, logical_recorded_at_ns
    inclusion, precondition gate, normalized_final_state_hash) from SS1-6
    ledger arithmetic entirely. Used both standalone (audit() must accept an
    empty input) and as the base for the §7 v2 replay/manifest tests below.

    `wall_clock_offset_ns` simulates "this run replayed on a different real
    day": added to every event's recorded_at_ns (excluded by normalization)
    while logical_recorded_at_ns (included) stays pinned to event_time_ns
    regardless -- this is the concrete case normalized_projection() exists
    to handle. `mutation` injects exactly one deviation, same pattern as
    build_scenario()'s `mutation` parameter.
    """
    b = Builder(run_id=run_id, schema_version=schema_version) if run_id is not None else Builder(schema_version=schema_version)

    def wc(event_time_ns):
        return dict(recorded_at_ns=wall_clock_offset_ns + event_time_ns, logical_recorded_at_ns=event_time_ns)

    rs_id = b.add("run_started", 0, 0, payload={
        "initial_cash": d("10000.00"), "initial_position": d("0"),
        "fee_model": "flat", "slippage_model": "worst-of-book",
        "stale_after_ns": 5_000_000_000,
        "config_hash": config_hash,
        "code_hash": code_hash,
        "rng_seed": rng_seed + 1 if mutation == "bad_rng_seed" else rng_seed,
        "replay_group_id": replay_group_id,
    }, **wc(0))

    ia_payload = {
        "dataset_hash": dataset_hash + "-OTHER" if mutation == "bad_dataset_hash" else dataset_hash,
        "dataset_schema_version": "1",
        "source": "synthetic-empty",
        "event_count": 0,
        "first_received_ts_ns": None,
        "last_received_ts_ns": None,
        "canonicalization_version": "1",
        "ordering_rule": "received_ts_ns,event_id",
        "dedup_rule": "event_id",
    }
    if mutation == "input_attached_missing_field":
        del ia_payload["dataset_hash"]
    ia_id = b.add("input_attached", 0, 0, caused_by=[rs_id], payload=ia_payload, **wc(0))

    rf_normalized_final_state_hash = normalized_final_state_hash + (
        "-DIVERGED" if mutation == "diverge_final_state" else ""
    )
    b.add("run_finished", 10, 10, caused_by=[ia_id], payload={
        "final_state_hash": final_state_hash,   # raw §5 -- run-specific, excluded from normalization
        "event_count": len(b.events) + 1,
        "journal_head_hash": b.events[-1]["event_hash"],  # raw §5 pointer -- excluded from normalization
        "normalized_final_state_hash": rf_normalized_final_state_hash,
    }, **wc(10))
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
    "valid empty_run (run_started -> input_attached -> run_finished)": lambda: build_empty_run(),
    "input_attached_missing_field (dataset_hash omitted)": lambda: build_empty_run(mutation="input_attached_missing_field"),
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
    "valid empty_run (run_started -> input_attached -> run_finished)": None,
    # missing field -> dget() raises INV_FORMAT (consistent with every other
    # required-field check in this file); INV_INPUT_ATTACHED is reserved for
    # the null/event_count consistency rule once all fields ARE present.
    "input_attached_missing_field (dataset_hash omitted)": audit.INV_FORMAT,
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


# ---------------------------------------------------------------------------
# DR-004 §7 v2: normalized replay comparison, checked as a comparison
# between TWO journals -- and manifest verification, checked against a
# GROUP of journals plus a separate manifest file. Both are claims about
# MULTIPLE artifacts, not one journal, so they get their own proof functions
# rather than a bolt-on to run() above.
#
# Supersedes the old (pre-DR-004) SS7 proof: v1 compared journals bitwise
# and needed an --allow-run-id-diff escape hatch + an INPUT/DERIVED
# event-type split to tell "wrong pair" from "nondeterministic". DR-004
# replaces both with (a) an explicit precondition gate (rng_seed/
# config_hash/code_hash/dataset_hash/schema_version, read from
# run_started/input_attached) and (b) normalized_replay_hash per event.
# build_scenario() (used by the retired v1 tests) predates input_attached
# and rng_seed-in-run_started, so it can no longer serve as a replay-pair
# fixture under the new preconditions; build_empty_run() is the DR-004-
# shaped replacement. Coverage carried forward: identical-input-identical-
# output, identical-input-different-output (nondeterminism), different-input
# (mismatched-input). "different run_id still compares" -- the old strict-
# vs-`--allow-run-id-diff` pair -- is now just replay_norm_identical itself:
# normalization excludes run_id unconditionally, no flag exists any more.
# ---------------------------------------------------------------------------

def _write_pair(tmpdir, name, builder_a, builder_b):
    path_a = tmpdir / (name + "_a.jsonl")
    path_b = tmpdir / (name + "_b.jsonl")
    builder_a.write(path_a)
    builder_b.write(path_b)
    return path_a, path_b


def run_replay_tests():
    tmpdir = Path(tempfile.mkdtemp(prefix="papertrade-audit-replay-test-"))
    results = []

    # 1. Different run_id, common replay_group_id, identical normalized
    #    content, but DIFFERENT recorded_at_ns (simulated wall-clock offset --
    #    exactly the case normalization exists to ignore) -> clean OK.
    name = "SS7v2 replay_norm_identical (different run_id/wall-clock, same normalized trace)"
    path_a, path_b = _write_pair(
        tmpdir, "replay_norm_identical",
        build_empty_run(run_id="run-A", wall_clock_offset_ns=1_000_000_000_000),
        build_empty_run(run_id="run-B", wall_clock_offset_ns=2_000_000_000_000),
    )
    try:
        n = audit.replay_compare(str(path_a), str(path_b))
        ok, outcome, detail = True, "PASS", f"OK, {n} events match"
    except audit.AuditViolation as v:
        ok, outcome, detail = False, "FAIL", f"expected OK but got [{v.invariant}]: {v.message}"
    results.append((name, ok, outcome, detail))

    # 2. Same preconditions (rng_seed/config_hash/code_hash/dataset_hash/
    #    schema_version all match), but the DERIVED run_finished disagrees on
    #    normalized_final_state_hash -> real §7 violation, pinned to run_finished.
    name = "SS7v2 replay_norm_nondeterministic (final state diverges on identical input)"
    path_a, path_b = _write_pair(
        tmpdir, "replay_norm_nondeterministic",
        build_empty_run(run_id="run-A"),
        build_empty_run(run_id="run-B", mutation="diverge_final_state"),
    )
    try:
        audit.replay_compare(str(path_a), str(path_b))
        ok, outcome, detail = False, "FAIL", "expected INV_7 violation but replay_compare accepted the pair"
    except audit.AuditViolation as v:
        if v.invariant == audit.INV_7 and v.event_type == "run_finished" and "недетерминирован" in v.message:
            ok, outcome, detail = True, "PASS", f"caught as [{v.invariant}] on run_finished: {v.message}"
        else:
            ok, outcome, detail = False, "FAIL", f"wrong classification: [{v.invariant}] event_type={v.event_type}: {v.message}"
    results.append((name, ok, outcome, detail))

    # 3. Different rng_seed -> precondition gate rejects the pair up front as
    #    mismatched-input, never reaches per-event comparison.
    name = "SS7v2 replay_norm_mismatched_input (rng_seed differs -> not a replay pair)"
    path_a, path_b = _write_pair(
        tmpdir, "replay_norm_mismatched_input",
        build_empty_run(run_id="run-A"),
        build_empty_run(run_id="run-B", mutation="bad_rng_seed"),
    )
    try:
        audit.replay_compare(str(path_a), str(path_b))
        ok, outcome, detail = False, "FAIL", "expected INV_7 violation but replay_compare accepted the pair"
    except audit.AuditViolation as v:
        if v.invariant == audit.INV_7 and "mismatched-input" in v.message:
            ok, outcome, detail = True, "PASS", f"caught as mismatched-input: {v.message}"
        else:
            ok, outcome, detail = False, "FAIL", f"expected mismatched-input classification, got [{v.invariant}]: {v.message}"
    results.append((name, ok, outcome, detail))

    return results


# ---------------------------------------------------------------------------
# DR-004 manifest verification: the auditor must not trust a single field of
# a manifest -- it independently recomputes everything from the journals.
# _correct_manifest() builds a manifest that IS accurate for a given set of
# (already-built) Builders, the same way build_scenario()'s honest path
# builds a journal that's valid "by construction". Each tampered-* test then
# corrupts exactly ONE manifest field, mirroring the `mutation` pattern used
# throughout this file for journals.
# ---------------------------------------------------------------------------

def _correct_manifest(builders, replay_group_id="rg-1"):
    runs = []
    trace_hashes = []
    for b in builders:
        events = b.events
        trace_hash = audit.compute_run_normalized_trace_hash(events)
        trace_hashes.append(trace_hash)
        runs.append({
            "run_id": b.run_id,
            "journal_head_hash": events[-1]["event_hash"],
            "event_count": len(events),
            "normalized_replay_hash": trace_hash,
        })
    rs_payload = builders[0].events[0]["payload"]
    ia_payload = next(e for e in builders[0].events if e["event_type"] == "input_attached")["payload"]
    return {
        "replay_group_id": replay_group_id,
        "runs": runs,
        "dataset_hash": ia_payload["dataset_hash"],
        "config_hash": rs_payload["config_hash"],
        "code_hash": rs_payload["code_hash"],
        "rng_seed": rs_payload["rng_seed"],
        "schema_version": builders[0].events[0]["schema_version"],
        "normalization_version": audit.NORMALIZATION_VERSION,
        "replay_equal": len(set(trace_hashes)) <= 1,
    }


def run_manifest_tests():
    tmpdir = Path(tempfile.mkdtemp(prefix="papertrade-audit-manifest-test-"))
    results = []

    def write_manifest(name, manifest):
        path = tmpdir / (name + "_manifest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, sort_keys=True)
        return path

    # 1. Honest pair + honest manifest -> verify_manifest accepts it.
    name = "manifest_ok (honest pair, honest manifest)"
    builder_a = build_empty_run(run_id="run-A")
    builder_b = build_empty_run(run_id="run-B")
    path_a, path_b = _write_pair(tmpdir, "manifest_ok", builder_a, builder_b)
    manifest = _correct_manifest([builder_a, builder_b])
    manifest_path = write_manifest("manifest_ok", manifest)
    try:
        result = audit.verify_manifest(str(manifest_path), [str(path_a), str(path_b)])
        ok, outcome, detail = True, "PASS", f"OK, replay_equal={result['replay_equal']}"
    except audit.AuditViolation as v:
        ok, outcome, detail = False, "FAIL", f"expected OK but got [{v.invariant}]: {v.message}"
    results.append((name, ok, outcome, detail))

    # 2. Manifest claims a normalized_replay_hash that doesn't match what the
    #    auditor independently recomputes from the journal -> INV_7.
    name = "manifest_tampered_hash (claimed normalized_replay_hash is wrong)"
    builder_a = build_empty_run(run_id="run-A")
    builder_b = build_empty_run(run_id="run-B")
    path_a, path_b = _write_pair(tmpdir, "manifest_tampered_hash", builder_a, builder_b)
    manifest = _correct_manifest([builder_a, builder_b])
    manifest["runs"][0]["normalized_replay_hash"] = "0" * 64
    manifest_path = write_manifest("manifest_tampered_hash", manifest)
    try:
        audit.verify_manifest(str(manifest_path), [str(path_a), str(path_b)])
        ok, outcome, detail = False, "FAIL", "expected INV_7 violation but verify_manifest accepted the manifest"
    except audit.AuditViolation as v:
        if v.invariant == audit.INV_7 and "normalized_replay_hash" in v.message:
            ok, outcome, detail = True, "PASS", f"caught as [{v.invariant}]: {v.message}"
        else:
            ok, outcome, detail = False, "FAIL", f"wrong classification: [{v.invariant}]: {v.message}"
    results.append((name, ok, outcome, detail))

    # 3. Manifest claims a journal_head_hash that doesn't match the journal's
    #    actual last event_hash -> INV_7.
    name = "manifest_wrong_head (claimed journal_head_hash is wrong)"
    builder_a = build_empty_run(run_id="run-A")
    builder_b = build_empty_run(run_id="run-B")
    path_a, path_b = _write_pair(tmpdir, "manifest_wrong_head", builder_a, builder_b)
    manifest = _correct_manifest([builder_a, builder_b])
    manifest["runs"][0]["journal_head_hash"] = "f" * 64
    manifest_path = write_manifest("manifest_wrong_head", manifest)
    try:
        audit.verify_manifest(str(manifest_path), [str(path_a), str(path_b)])
        ok, outcome, detail = False, "FAIL", "expected INV_7 violation but verify_manifest accepted the manifest"
    except audit.AuditViolation as v:
        if v.invariant == audit.INV_7 and "journal_head_hash" in v.message:
            ok, outcome, detail = True, "PASS", f"caught as [{v.invariant}]: {v.message}"
        else:
            ok, outcome, detail = False, "FAIL", f"wrong classification: [{v.invariant}]: {v.message}"
    results.append((name, ok, outcome, detail))

    # 4. The pair actually DIVERGES (nondeterministic), and each per-run
    #    normalized_replay_hash the manifest declares is honestly correct --
    #    but the manifest still lies and claims replay_equal=true -> INV_7.
    name = "manifest_lies_replay_equal (traces differ but manifest claims replay_equal=true)"
    builder_a = build_empty_run(run_id="run-A")
    builder_b = build_empty_run(run_id="run-B", mutation="diverge_final_state")
    path_a, path_b = _write_pair(tmpdir, "manifest_lies_replay_equal", builder_a, builder_b)
    manifest = _correct_manifest([builder_a, builder_b])
    assert manifest["replay_equal"] is False, "fixture bug: expected these two traces to genuinely differ"
    manifest["replay_equal"] = True  # the lie
    manifest_path = write_manifest("manifest_lies_replay_equal", manifest)
    try:
        audit.verify_manifest(str(manifest_path), [str(path_a), str(path_b)])
        ok, outcome, detail = False, "FAIL", "expected INV_7 violation but verify_manifest accepted the manifest"
    except audit.AuditViolation as v:
        if v.invariant == audit.INV_7 and "replay_equal" in v.message:
            ok, outcome, detail = True, "PASS", f"caught as [{v.invariant}]: {v.message}"
        else:
            ok, outcome, detail = False, "FAIL", f"wrong classification: [{v.invariant}]: {v.message}"
    results.append((name, ok, outcome, detail))

    return results


def main():
    results = run() + run_replay_tests() + run_manifest_tests()

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
