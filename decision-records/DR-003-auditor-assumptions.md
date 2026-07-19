# DR-003 — допущения аудитора по SCHEMA (praxis подтвердить при генерации журнала)

**Дата:** 2026-07-19. **Статус:** предложено. Аудитор `audit/audit.py` построен и протестирован
на синтетике 19.07 (валидный проходит, каждое нарушение §1-7 ловится). При реализации всплыли
неоднозначности SCHEMA — зафиксированы как допущения аудитора; симулятор praxis должен совпасть либо
мы правим SCHEMA новым DR.

## Допущение 1 — граница input_head_hash
Видимый срез решения ограничен по **observed_through_received_ts_ns** (SCHEMA прямо зовёт его «граница
видимого рынка»), НЕ по decision_time_ns. input_head_hash = event_hash последнего market_quote с
received_ts_ns ≤ observed_through_received_ts_ns. Разница проявится только если observed_through заметно
меньше decision_time_ns (задержка обработки). **praxis: считать input_head_hash так же.**

## Допущение 2 — структура конверта
Поля события (market_quote/decision/…) лежат во ВЛОЖЕННОМ `payload: {...}` внутри общего конверта
(SCHEMA перечисляет payload как поле конверта). Если praxis сделает плоско — правка в одном месте
аудитора (dget / DECIMAL_PAYLOAD_FIELDS). **praxis: подтвердить nested payload.**

## Допущение 3 — replay-пара и run_id (§7 детерминизм)
§7 реализован как `replay_compare(A, B)` (audit/audit.py): оба журнала проходят §1-6, затем сверяются
событие-в-событие. Расхождение во ВХОДНОМ событии (run_started/market_quote/decision) = «разные прогоны,
не replay» (mismatched-input); расхождение в ПРОИЗВОДНОМ (order/fill/account_state/run_finished) при
совпавшем входе = нарушение §7. Протестировано на синтетике (identical / nondeterministic / different-input /
run_id-diff).

**Открытый вопрос praxis: replay-пара пишется с ТЕМ ЖЕ run_id или разным?**
- ТОТ ЖЕ run_id → строгое побитовое сравнение полно и просто (флаг не нужен). **Рекомендуем этот путь.**
- РАЗНЫЙ run_id → нужен `--allow-run-id-diff`, но он ПОКА НЕПОЛОН: исключает run_id/prev_hash, а
  payload-указатели на хэши (input_head_hash, journal_head_hash, final_state_hash, state_*_hash)
  транзитивно зависят от run_id через canonical_hash и разойдутся закономерно. Если praxis выберет разный
  run_id — доработать флаг под ПЕРЕЧЕНЬ hash-указателей от praxis. До подтверждения — писать тот же run_id.

## Отложено (не в v0-аудите)
- equity / unrealized_pnl в account_state — не аудируется: в SCHEMA нет соглашения о mark-price.
