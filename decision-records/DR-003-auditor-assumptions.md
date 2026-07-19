# DR-003 — допущения аудитора по SCHEMA (praxis подтвердить при генерации журнала)

**Дата:** 2026-07-19. **Статус:** предложено. Аудитор `audit/audit.py` построен и протестирован
на синтетике 19.07 (валидный проходит, каждое нарушение §1-6 ловится). При реализации всплыли две
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

## Отложено (не в v0-аудите)
- §7 детерминизм replay — нужен ЗАПУСК двух прогонов + побитовое сравнение, не чтение одного журнала. deferred.
- equity / unrealized_pnl в account_state — не аудируется: в SCHEMA нет соглашения о mark-price.
