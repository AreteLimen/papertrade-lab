#!/usr/bin/env python3
"""Independent journal auditor for papertrade-lab.

WHY this exists (see ../SCHEMA.md and ../README.md): the simulator (praxis)
and the auditor (Arete) must not share assumptions. This file never imports
or calls into simulator code — it only reads the JSONL journal as a plain
artifact and recomputes everything it needs from scratch (hash chain,
cash/position ledger, execution-price bounds). If the simulator lies, the
lie has to survive re-derivation from raw fields, not just "look consistent".

Exit code: 0 = journal clean, 1 = an invariant was violated (message on
stderr explains which one, at which seq/event).
"""

import argparse
import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation

GENESIS_HASH = "0" * 64  # prev_hash of the very first event in a run

# Which invariant families exist, purely for labeling violations consistently
# with SCHEMA.md's numbering.
INV_FORMAT = "FORMAT (no float)"
INV_1 = "SS1 no look-ahead"
INV_2 = "SS2 fill timing / first quote"
INV_3 = "SS3 execution worse than market"
INV_4 = "SS4 no infinite liquidity"
INV_5 = "SS5 append-only (hash chain / seq)"
INV_6 = "SS6 account_state is derived"


class AuditViolation(Exception):
    """Raised on the first invariant breach found. Carries enough context
    to point a human straight at the offending line without re-deriving it."""

    def __init__(self, invariant, message, seq=None, event_id=None, event_type=None):
        self.invariant = invariant
        self.message = message
        self.seq = seq
        self.event_id = event_id
        self.event_type = event_type
        super().__init__(str(self))

    def __str__(self):
        loc = []
        if self.seq is not None:
            loc.append(f"seq={self.seq}")
        if self.event_type is not None:
            loc.append(f"event_type={self.event_type}")
        if self.event_id is not None:
            loc.append(f"event_id={self.event_id}")
        loc_str = f" [{', '.join(loc)}]" if loc else ""
        return f"AUDIT VIOLATION [{self.invariant}]{loc_str}: {self.message}"


def _ctx(event):
    return dict(seq=event.get("seq"), event_id=event.get("event_id"), event_type=event.get("event_type"))


# ---------------------------------------------------------------------------
# Canonical hashing — MUST match exactly what a compliant writer computes.
# Canonical form: the full event dict minus "event_hash", dumped with
# sort_keys so field order in the source file can never change the hash.
# ---------------------------------------------------------------------------

def canonical_hash(event: dict) -> str:
    body = {k: v for k, v in event.items() if k != "event_hash"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Field-level number discipline: "integers in minimal units OR decimal
# strings, NEVER float" (SCHEMA.md header). Enforced field-by-field so a
# single stray float anywhere in the journal is caught immediately, not just
# for fields we happen to use in arithmetic.
# ---------------------------------------------------------------------------

def require_decimal(value, field, event):
    if isinstance(value, bool) or isinstance(value, float):
        raise AuditViolation(
            INV_FORMAT,
            f"field '{field}' = {value!r} is a float/bool — money and quantity fields must be "
            f"int (minimal units) or a decimal string",
            **_ctx(event),
        )
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            raise AuditViolation(
                INV_FORMAT, f"field '{field}' = {value!r} is not a parseable decimal string", **_ctx(event)
            )
    raise AuditViolation(
        INV_FORMAT, f"field '{field}' has unsupported type {type(value).__name__}", **_ctx(event)
    )


def require_int(value, field, event):
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuditViolation(INV_FORMAT, f"field '{field}' = {value!r} must be an int, not float/other", **_ctx(event))
    return value


def dget(payload, field, event):
    if field not in payload:
        raise AuditViolation(INV_FORMAT, f"payload missing required field '{field}'", **_ctx(event))
    return payload[field]


# Money/quantity fields per event type that must pass require_decimal.
DECIMAL_PAYLOAD_FIELDS = {
    "market_quote": ["bid_price", "bid_qty", "ask_price", "ask_qty"],
    "decision": ["requested_qty"],
    "order_submitted": ["requested_qty"],  # limit_price checked separately (optional)
    "order_rejected": ["balance_before", "position_before"],
    "fill": [
        "filled_qty", "book_price", "slippage_amount", "execution_price",
        "available_qty", "fee", "cash_delta", "position_delta",
    ],
    "account_state": [
        "cash_before", "cash_after", "position_before", "position_after",
        "avg_entry_price", "realized_pnl", "unrealized_pnl", "equity",
    ],
    "run_started": ["initial_cash", "initial_position"],
}

# ns-timestamp / count fields that must be plain ints (per event type, payload only;
# envelope ns fields are checked directly in the main loop).
INT_PAYLOAD_FIELDS = {
    "market_quote": ["exchange_ts_ns", "received_ts_ns"],
    "decision": ["decision_time_ns", "observed_through_received_ts_ns"],
    "order_submitted": ["submitted_ts_ns"],
    "run_finished": ["event_count"],
}


def validate_field_formats(event):
    """SS FORMAT: walk the declared numeric fields for this event_type and make
    sure none of them snuck in as float. Unknown event_types are left alone
    (nothing in SCHEMA.md to check them against)."""
    etype = event["event_type"]
    payload = event.get("payload", {})
    for field in DECIMAL_PAYLOAD_FIELDS.get(etype, []):
        if field in payload:  # optional fields (e.g. limit_price) handled at call site
            require_decimal(payload[field], field, event)
    for field in INT_PAYLOAD_FIELDS.get(etype, []):
        if field in payload:
            require_int(payload[field], field, event)
    if etype == "order_submitted" and payload.get("limit_price") is not None:
        require_decimal(payload["limit_price"], "limit_price", event)


# ---------------------------------------------------------------------------
# Ledger: independent cash/position/avg-entry/realized-pnl reconstruction.
# Shared between audit.py's SS6 check and the synthetic-journal generator in
# test_audit.py, so the "expected" values used to build a valid test journal
# and the values the auditor recomputes come from the exact same arithmetic
# — any divergence in a test is therefore a real bug, not formula drift.
#
# Simplification (documented, not hidden): average-cost basis, no lot-level
# FIFO/LIFO. Good enough for v0 (single symbol, no position flips within one
# fill). See report to Evgeniy for what this does not cover.
# ---------------------------------------------------------------------------

class Ledger:
    def __init__(self, initial_cash: Decimal, initial_position: Decimal):
        self.cash = initial_cash
        self.position = initial_position
        self.avg_entry_price = Decimal(0)
        self.realized_pnl = Decimal(0)

    def snapshot(self):
        return (self.cash, self.position, self.avg_entry_price, self.realized_pnl)

    def apply_fill(self, side: str, filled_qty: Decimal, execution_price: Decimal, fee: Decimal):
        """Mutates state in place using ONLY side/filled_qty/execution_price/fee —
        never the fill's own declared cash_delta/position_delta/realized_pnl.
        Returns (expected_cash_delta, expected_position_delta) so callers can
        cross-check the journal's declared deltas against reality."""
        signed_qty = filled_qty if side == "buy" else -filled_qty
        if side == "buy":
            expected_cash_delta = -(execution_price * filled_qty) - fee
        else:
            expected_cash_delta = (execution_price * filled_qty) - fee
        expected_position_delta = signed_qty

        old_position = self.position
        new_position = old_position + signed_qty

        def sign(x: Decimal) -> int:
            if x > 0:
                return 1
            if x < 0:
                return -1
            return 0

        if old_position == 0 or sign(new_position) == sign(old_position) and abs(new_position) >= abs(old_position):
            # opening from flat, or adding to an existing position in the same direction
            total_qty = abs(old_position) + filled_qty
            if total_qty != 0:
                self.avg_entry_price = (
                    (self.avg_entry_price * abs(old_position)) + (execution_price * filled_qty)
                ) / total_qty
            realized_delta = Decimal(0)
        elif new_position == 0:
            # fully closes the existing position
            realized_delta = (execution_price - self.avg_entry_price) * filled_qty * sign(old_position)
            self.avg_entry_price = Decimal(0)
        elif sign(new_position) == sign(old_position):
            # partial close, same direction retained
            realized_delta = (execution_price - self.avg_entry_price) * filled_qty * sign(old_position)
            # avg_entry_price unchanged
        else:
            # position flips sign in one fill: close old leg fully, open new leg at execution_price
            closed_qty = abs(old_position)
            realized_delta = (execution_price - self.avg_entry_price) * closed_qty * sign(old_position)
            self.avg_entry_price = execution_price

        self.realized_pnl += realized_delta
        self.cash += expected_cash_delta
        self.position = new_position
        return expected_cash_delta, expected_position_delta


# ---------------------------------------------------------------------------
# Journal loading + SS5 (append-only) + generic per-event structural checks.
# ---------------------------------------------------------------------------

REQUIRED_ENVELOPE_FIELDS = [
    "schema_version", "run_id", "seq", "event_id", "event_type",
    "event_time_ns", "received_ts_ns", "recorded_at_ns", "caused_by",
    "prev_hash", "payload", "event_hash",
]


def load_and_check_chain(path):
    """Pass 1: parse every line, verify the SS5 hash chain and seq counter,
    verify number-format discipline, verify caused_by only points backward
    in time to events that actually appear earlier in the file. Stops at the
    very first structural problem — nothing downstream can be trusted once
    the chain itself is broken, so semantic checks (SS1-4, SS6) run in pass 2
    only after this pass has fully succeeded.

    Returns (events, id_index, event_time_index).
    """
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                raise AuditViolation(INV_FORMAT, f"line {lineno} is not valid JSON: {e}")
            for field in REQUIRED_ENVELOPE_FIELDS:
                if field not in event:
                    raise AuditViolation(
                        INV_FORMAT, f"line {lineno} missing envelope field '{field}'",
                        seq=event.get("seq"), event_id=event.get("event_id"), event_type=event.get("event_type"),
                    )
            events.append(event)

    if not events:
        raise AuditViolation(INV_5, "journal is empty — nothing to audit")

    id_index = {}
    event_time_index = {}
    running_prev_hash = GENESIS_HASH
    expected_seq = None

    for event in events:
        ctx = _ctx(event)

        # --- envelope ns/seq fields must be ints ---
        seq = require_int(event["seq"], "seq", event)
        require_int(event["event_time_ns"], "event_time_ns", event)
        require_int(event["received_ts_ns"], "received_ts_ns", event)
        require_int(event["recorded_at_ns"], "recorded_at_ns", event)

        # --- SS5: seq strictly +1, no gaps ---
        if expected_seq is None:
            expected_seq = seq
        elif seq != expected_seq + 1:
            raise AuditViolation(
                INV_5, f"seq gap: expected {expected_seq + 1}, got {seq}", **ctx
            )
        expected_seq = seq

        # --- SS5: prev_hash must equal the PREVIOUS event's recorded event_hash ---
        if event["prev_hash"] != running_prev_hash:
            raise AuditViolation(
                INV_5,
                f"prev_hash mismatch: expected {running_prev_hash}, got {event['prev_hash']} "
                f"(chain broken / event reordered or deleted)",
                **ctx,
            )

        # --- SS5: event_hash must equal hash of this event's own canonical content ---
        recomputed = canonical_hash(event)
        if event["event_hash"] != recomputed:
            raise AuditViolation(
                INV_5,
                f"event_hash mismatch: stored={event['event_hash']} recomputed={recomputed} "
                f"(payload was edited after being hashed — history was rewritten)",
                **ctx,
            )
        running_prev_hash = event["event_hash"]

        # --- number-format discipline (payload-level) ---
        validate_field_formats(event)

        # --- SS1 (partial): caused_by only points backward, to events that exist ---
        for cause_id in event.get("caused_by", []):
            if cause_id not in id_index:
                raise AuditViolation(
                    INV_1,
                    f"caused_by references '{cause_id}' which does not appear earlier in the journal "
                    f"(unknown event or forward reference)",
                    **ctx,
                )
            cause_time = event_time_index[cause_id]
            if cause_time > event["event_time_ns"]:
                raise AuditViolation(
                    INV_1,
                    f"caused_by references '{cause_id}' (event_time_ns={cause_time}) which is LATER than "
                    f"this event's event_time_ns={event['event_time_ns']} — a cause cannot happen after its effect",
                    **ctx,
                )

        eid = event["event_id"]
        if eid in id_index:
            raise AuditViolation(INV_5, f"duplicate event_id '{eid}'", **ctx)
        id_index[eid] = event
        event_time_index[eid] = event["event_time_ns"]

    return events, id_index, event_time_index


# ---------------------------------------------------------------------------
# Pass 2: semantic invariants (SS1 rest, SS2, SS3, SS4, SS6).
# ---------------------------------------------------------------------------

def run_semantic_checks(events, id_index):
    market_quotes = [e for e in events if e["event_type"] == "market_quote"]
    market_quotes_sorted = sorted(market_quotes, key=lambda e: (e["payload"]["received_ts_ns"], e["seq"]))
    orders_by_id = {e["payload"]["order_id"]: e for e in events if e["event_type"] == "order_submitted"}

    ledger = None
    # event_id (of the fill/order_rejected that will trigger an account_state) -> expected tuple
    pending_state = {}

    for event in events:
        etype = event["event_type"]
        payload = event["payload"]
        ctx = _ctx(event)

        if etype == "run_started":
            initial_cash = require_decimal(dget(payload, "initial_cash", event), "initial_cash", event)
            initial_position = require_decimal(dget(payload, "initial_position", event), "initial_position", event)
            ledger = Ledger(initial_cash, initial_position)

        elif etype == "decision":
            observed_through = payload["observed_through_received_ts_ns"]
            decision_time = payload["decision_time_ns"]
            if observed_through > decision_time:
                raise AuditViolation(
                    INV_1,
                    f"observed_through_received_ts_ns={observed_through} is AFTER "
                    f"decision_time_ns={decision_time} — decision claims to have seen the future",
                    **ctx,
                )
            # Reconstruct the visible slice: the last market_quote with
            # received_ts_ns <= observed_through_received_ts_ns must be the one
            # input_head_hash points to. (Boundary = observed_through, per
            # SCHEMA.md's own comment that this field IS "the visible-market
            # boundary" — see report for why this differs from a literal
            # reading of decision_time_ns.)
            visible = [q for q in market_quotes_sorted if q["payload"]["received_ts_ns"] <= observed_through]
            if visible:
                expected_head = visible[-1]["event_hash"]
                if payload.get("input_head_hash") != expected_head:
                    raise AuditViolation(
                        INV_1,
                        f"input_head_hash={payload.get('input_head_hash')} does not match the hash of the "
                        f"last visible event ({visible[-1]['event_id']}, hash={expected_head}) for "
                        f"observed_through_received_ts_ns={observed_through} — decision's declared input "
                        f"does not match the reconstructed visible slice",
                        **ctx,
                    )

        elif etype == "order_submitted":
            pass  # nothing to check standalone; consumed by fill below

        elif etype == "order_rejected":
            if ledger is None:
                raise AuditViolation(INV_6, "order_rejected before run_started", **ctx)
            balance_before = require_decimal(dget(payload, "balance_before", event), "balance_before", event)
            position_before = require_decimal(dget(payload, "position_before", event), "position_before", event)
            if balance_before != ledger.cash:
                raise AuditViolation(
                    INV_6,
                    f"order_rejected.balance_before={balance_before} does not match independently "
                    f"reconstructed cash={ledger.cash}",
                    **ctx,
                )
            if position_before != ledger.position:
                raise AuditViolation(
                    INV_6,
                    f"order_rejected.position_before={position_before} does not match independently "
                    f"reconstructed position={ledger.position}",
                    **ctx,
                )
            snap = ledger.snapshot()
            pending_state[event["event_id"]] = (snap[0], snap[0], snap[1], snap[1], snap[2], snap[3])

        elif etype == "fill":
            if ledger is None:
                raise AuditViolation(INV_6, "fill before run_started", **ctx)

            order_id = dget(payload, "order_id", event)
            order = orders_by_id.get(order_id)
            if order is None:
                raise AuditViolation(INV_2, f"fill references unknown order_id={order_id!r}", **ctx)
            order_payload = order["payload"]
            submitted_ts_ns = order_payload["submitted_ts_ns"]
            side = dget(payload, "side", event)

            quote_event_id = dget(payload, "quote_event_id", event)
            quote = id_index.get(quote_event_id)
            if quote is None or quote["event_type"] != "market_quote":
                raise AuditViolation(INV_2, f"fill.quote_event_id={quote_event_id!r} is not a known market_quote", **ctx)
            qpayload = quote["payload"]

            # --- SS2: fill only after submission ---
            if qpayload["received_ts_ns"] < submitted_ts_ns:
                raise AuditViolation(
                    INV_2,
                    f"fill's quote received_ts_ns={qpayload['received_ts_ns']} is BEFORE "
                    f"order_submitted.submitted_ts_ns={submitted_ts_ns} (order_id={order_id}) — "
                    f"filled on a quote that predates the order",
                    **ctx,
                )

            # --- SS2: first suitable quote, not a cherry-picked later one ---
            order_type = order_payload.get("order_type", "market")
            price_field = "ask_price" if side == "buy" else "bid_price"
            candidates = [q for q in market_quotes_sorted if q["payload"]["received_ts_ns"] >= submitted_ts_ns]
            if order_type == "limit":
                limit_price = require_decimal(order_payload.get("limit_price"), "limit_price", order)
                filtered = []
                for q in candidates:
                    qp = require_decimal(q["payload"][price_field], price_field, q)
                    if (side == "buy" and qp <= limit_price) or (side == "sell" and qp >= limit_price):
                        filtered.append(q)
                candidates = filtered
            if not candidates:
                raise AuditViolation(INV_2, f"no eligible quote found at/after submit for order_id={order_id}", **ctx)
            first_eligible = candidates[0]
            if first_eligible["event_id"] != quote_event_id:
                raise AuditViolation(
                    INV_2,
                    f"fill used quote {quote_event_id} but the first eligible quote after submission "
                    f"was {first_eligible['event_id']} (received_ts_ns={first_eligible['payload']['received_ts_ns']}) "
                    f"— a later, presumably more favorable, quote was cherry-picked",
                    **ctx,
                )

            # --- SS3: execution worse than market; fee >= 0; slippage magnitude consistent ---
            execution_price = require_decimal(dget(payload, "execution_price", event), "execution_price", event)
            book_price = require_decimal(dget(payload, "book_price", event), "book_price", event)
            fee = require_decimal(dget(payload, "fee", event), "fee", event)
            slippage_amount = require_decimal(dget(payload, "slippage_amount", event), "slippage_amount", event)

            market_side_price = require_decimal(qpayload[price_field], price_field, quote)
            if book_price != market_side_price:
                raise AuditViolation(
                    INV_3,
                    f"fill.book_price={book_price} does not match quote.{price_field}={market_side_price} "
                    f"for side={side}",
                    **ctx,
                )
            if side == "buy":
                if execution_price < book_price:
                    raise AuditViolation(
                        INV_3,
                        f"buy execution_price={execution_price} is BETTER than ask={book_price} "
                        f"— execution must never beat the market",
                        **ctx,
                    )
            elif side == "sell":
                if execution_price > book_price:
                    raise AuditViolation(
                        INV_3,
                        f"sell execution_price={execution_price} is BETTER than bid={book_price} "
                        f"— execution must never beat the market",
                        **ctx,
                    )
            else:
                raise AuditViolation(INV_3, f"unknown side={side!r}", **ctx)

            if fee < 0:
                raise AuditViolation(INV_3, f"fee={fee} is negative", **ctx)

            expected_slippage = abs(execution_price - book_price)
            if slippage_amount != expected_slippage:
                raise AuditViolation(
                    INV_3,
                    f"declared slippage_amount={slippage_amount} does not match "
                    f"|execution_price - book_price|={expected_slippage} — slippage sign/magnitude cannot be trusted "
                    f"as declared",
                    **ctx,
                )

            # --- SS4: no infinite liquidity ---
            filled_qty = require_decimal(dget(payload, "filled_qty", event), "filled_qty", event)
            available_qty = require_decimal(dget(payload, "available_qty", event), "available_qty", event)
            qty_field = "ask_qty" if side == "buy" else "bid_qty"
            book_qty = require_decimal(qpayload[qty_field], qty_field, quote)
            if available_qty != book_qty:
                raise AuditViolation(
                    INV_4,
                    f"fill.available_qty={available_qty} does not match quote.{qty_field}={book_qty}",
                    **ctx,
                )
            if filled_qty > available_qty:
                raise AuditViolation(
                    INV_4,
                    f"filled_qty={filled_qty} exceeds available_qty={available_qty} at the top of book "
                    f"— fill exceeds book depth (infinite liquidity assumed)",
                    **ctx,
                )
            if filled_qty <= 0:
                raise AuditViolation(INV_4, f"filled_qty={filled_qty} must be positive", **ctx)

            # --- SS6: cross-check the fill's own declared deltas against reality,
            # then advance the independent ledger using ONLY recomputed values ---
            cash_before = ledger.cash
            position_before = ledger.position
            expected_cash_delta, expected_position_delta = ledger.apply_fill(side, filled_qty, execution_price, fee)

            declared_cash_delta = require_decimal(dget(payload, "cash_delta", event), "cash_delta", event)
            declared_position_delta = require_decimal(dget(payload, "position_delta", event), "position_delta", event)
            if declared_cash_delta != expected_cash_delta:
                raise AuditViolation(
                    INV_6,
                    f"fill.cash_delta={declared_cash_delta} does not match recomputed "
                    f"-(execution_price*filled_qty)-fee-style delta={expected_cash_delta}",
                    **ctx,
                )
            if declared_position_delta != expected_position_delta:
                raise AuditViolation(
                    INV_6,
                    f"fill.position_delta={declared_position_delta} does not match recomputed "
                    f"delta={expected_position_delta}",
                    **ctx,
                )

            pending_state[event["event_id"]] = (
                cash_before, ledger.cash, position_before, ledger.position,
                ledger.avg_entry_price, ledger.realized_pnl,
            )

        elif etype == "account_state":
            triggered_by = dget(payload, "triggered_by", event)
            if triggered_by not in pending_state:
                raise AuditViolation(
                    INV_6,
                    f"account_state.triggered_by={triggered_by!r} does not match any preceding "
                    f"fill/order_rejected event_id",
                    **ctx,
                )
            exp_cash_before, exp_cash_after, exp_pos_before, exp_pos_after, exp_avg, exp_realized = pending_state[triggered_by]

            cash_before = require_decimal(dget(payload, "cash_before", event), "cash_before", event)
            cash_after = require_decimal(dget(payload, "cash_after", event), "cash_after", event)
            position_before = require_decimal(dget(payload, "position_before", event), "position_before", event)
            position_after = require_decimal(dget(payload, "position_after", event), "position_after", event)

            if cash_before != exp_cash_before:
                raise AuditViolation(INV_6, f"account_state.cash_before={cash_before} != reconstructed {exp_cash_before}", **ctx)
            if cash_after != exp_cash_after:
                raise AuditViolation(
                    INV_6,
                    f"account_state.cash_after={cash_after} != independently reconstructed cash={exp_cash_after} "
                    f"(triggered_by={triggered_by})",
                    **ctx,
                )
            if position_before != exp_pos_before:
                raise AuditViolation(INV_6, f"account_state.position_before={position_before} != reconstructed {exp_pos_before}", **ctx)
            if position_after != exp_pos_after:
                raise AuditViolation(INV_6, f"account_state.position_after={position_after} != reconstructed {exp_pos_after}", **ctx)

            if "avg_entry_price" in payload:
                avg = require_decimal(payload["avg_entry_price"], "avg_entry_price", event)
                if avg != exp_avg:
                    raise AuditViolation(INV_6, f"account_state.avg_entry_price={avg} != reconstructed {exp_avg}", **ctx)
            if "realized_pnl" in payload:
                realized = require_decimal(payload["realized_pnl"], "realized_pnl", event)
                if realized != exp_realized:
                    raise AuditViolation(INV_6, f"account_state.realized_pnl={realized} != reconstructed {exp_realized}", **ctx)
            # equity / unrealized_pnl deliberately NOT checked: SCHEMA.md does not define a
            # mark-price convention, so we have no independent way to derive them. See report.

        elif etype == "run_finished":
            event_count = payload.get("event_count")
            if event_count is not None and event_count != len(events):
                raise AuditViolation(
                    INV_5, f"run_finished.event_count={event_count} != actual event count {len(events)}", **ctx
                )
            journal_head_hash = payload.get("journal_head_hash")
            if journal_head_hash is not None:
                idx = events.index(event)
                if idx == 0:
                    raise AuditViolation(INV_5, "run_finished is the only event; no prior head to point to", **ctx)
                prev_event_hash = events[idx - 1]["event_hash"]
                if journal_head_hash != prev_event_hash:
                    raise AuditViolation(
                        INV_5,
                        f"run_finished.journal_head_hash={journal_head_hash} != hash of preceding event "
                        f"{prev_event_hash}",
                        **ctx,
                    )


def audit(path):
    events, id_index, _event_time_index = load_and_check_chain(path)
    run_semantic_checks(events, id_index)
    return len(events)


def main():
    parser = argparse.ArgumentParser(description="Independent auditor for papertrade-lab JSONL journals.")
    parser.add_argument("journal", help="path to the JSONL journal file")
    args = parser.parse_args()

    try:
        n = audit(args.journal)
    except AuditViolation as v:
        print(str(v), file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"AUDIT ERROR: journal not found: {args.journal}", file=sys.stderr)
        sys.exit(1)

    print(f"OK: {n} events, no invariant violations found.")
    sys.exit(0)


if __name__ == "__main__":
    main()
