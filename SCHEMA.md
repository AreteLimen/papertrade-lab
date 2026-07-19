# SCHEMA — контракт журнала (единственный источник правды по формату)

Сошёлся praxis (симулятор) + Arête (аудит), 2026-07-18. Меняется только через decision record.

Формат: **JSONL, одна строка = одно неизменяемое событие.** Деньги/объём — целые в минимальных
единицах ЛИБО decimal-строки. **Никаких float.**

## Общий конверт
    schema_version, run_id, seq, event_id, event_type,
    event_time_ns      # логическое время симулятора
    received_ts_ns     # граница знания рынка (когда МЫ получили)  -- разведены нарочно
    recorded_at_ns         # ФАКТИЧЕСКОЕ физическое время записи; ВНЕ нормализованного replay-сравнения (§7)
    logical_recorded_at_ns # детерминированное «когда записалось бы при идеальном replay»; ВХОДИТ в нормализацию (§7)
    caused_by[]        # event_id причин; только назад по event_time_ns
    prev_hash, payload, event_hash  # hash канонического события без event_hash

## События
    market_quote: symbol, exchange_ts_ns, received_ts_ns, bid_price, bid_qty,
                  ask_price, ask_qty, source, raw_payload_hash
    decision:     strategy_id, strategy_version, decision_time_ns,
                  observed_through_received_ts_ns,   # граница видимого рынка (по RECEIVED, не exchange)
                  input_head_hash,                   # СЫРОЙ §5 event_hash видимого среза (для §1/§5, внутрипрогонный)
                  normalized_input_head_hash,        # normalized_replay_hash того же среза (для §7 между прогонами; DR-004)
                  action, requested_qty, config_hash, code_hash, rng_seed
    order_submitted: order_id, decision_id, side, order_type, requested_qty,
                     limit_price?, submitted_ts_ns
    order_rejected:  order_id, reason_code, reason_detail, balance_before, position_before
    fill:         fill_id, order_id, quote_event_id, side, filled_qty,
                  book_price, slippage_amount, execution_price, available_qty,
                  fee, cash_delta, position_delta
    account_state (полностью производное): triggered_by, cash_before/after,
                  position_before/after, avg_entry_price, realized_pnl, unrealized_pnl,
                  equity, state_before_hash, state_after_hash
    run_started:  initial_cash, initial_position, fee_model, slippage_model,
                  stale_after_ns, config_hash, code_hash, rng_seed, replay_group_id
                  # replay_group_id связывает прогоны ОДНОЙ replay-пары (разные run_id); NULL — одиночный прогон
    input_attached: dataset_hash,            # хэш КАНОНИЗИРОВАННОГО содержимого входа, не файла-контейнера
                  dataset_schema_version, source,   # source: тип/идентификатор без секретов и локальных путей
                  event_count, first_received_ts_ns, last_received_ts_ns,
                  canonicalization_version, ordering_rule, dedup_rule
                  # UTC+ns фиксированы схемой. Пустой вход: диапазоны=null, event_count=0, хэш от канонич. пустого набора.
                  # exchange_ts-диапазон — информационно; причинная граница и сортировка ВСЕГДА по received_ts_ns.
    run_finished: final_state_hash, event_count, journal_head_hash,
                  normalized_final_state_hash   # для §7 replay; сырой final_state_hash — §5-целостность (DR-004)

## Инварианты (проверяет независимый аудитор)
1. **Нет look-ahead.** Решение — чистая функция (события с received_ts_ns <= decision_time_ns,
   config, code, rng_seed). Симулятор подаёт стратегии ТОЛЬКО этот срез. Replay сверяет action И
   input_head_hash — точный вход, не только выход.
2. **Fill только после подачи и по первой подходящей котировке.** fill.quote.received_ts_ns >=
   order_submitted.submitted_ts_ns; для рыночной — ПЕРВАЯ подходящая, не выбранная позже выгодная.
3. **Исполнение невыгоднее рынка.** buy по ask, sell по bid; знаку slippage не доверяем — аудитор
   сам проверяет execution_price невыгоднее book_price; fee >= 0.
4. **Нет бесконечной ликвидности.** filled_qty <= available_qty вершины стакана; нехватка -> частичный
   fill, остаток открыт либо отменён отдельным событием.
5. **Append-only.** prev_hash/event_hash — цепочка; правка прошлого её рвёт. seq строго растёт без дыр.
6. **account_state — результат, не истина.** Аудитор пересчитывает его независимо из fill/rejection.
7. **Детерминизм replay.** Два прогона ОДНОЙ replay-группы (разные run_id, общий replay_group_id) на том же
   входе (dataset_hash) при совпадении rng_seed / config_hash / code_hash / schema_version дают тождественный
   НОРМАЛИЗОВАННЫЙ след. Нормализация ИСКЛЮЧАЕТ идентификаторы экземпляра (run_id, prev_hash, event_hash) и
   ФАКТИЧЕСКОЕ физическое время (recorded_at_ns); logical_recorded_at_ns и весь детерминированный контент —
   ВКЛЮЧАЕТ. normalized_replay_hash = хэш нормализованной проекции; два прогона детерминистичны ⟺ их
   normalized_replay_hash совпадают пособытийно. Это ОТДЕЛЬНО от §5-цепи: event_hash держит целостность ВНУТРИ
   прогона (включает всё), §7 сравнивает МЕЖДУ прогонами по нормализованной проекции. Формат нормализации,
   manifest и правила сверки — **DR-004**.
