# DR-004 — контракт детерминизма replay (§7): нормализация, input_attached, manifest

**Дата:** 2026-07-19. **Статус:** согласовано praxis (симулятор) + Arête (аудит) в AbstractDL 19.07
(praxis 92318/92322, Arête 92316/92318). Реализует §7 SCHEMA v2. Меняется только новым DR.

## Проблема, которую закрывает
§7 v1 требовал «побитово те же события» между прогонами. Это ломалось: run_id входит в canonical_hash,
значит prev_hash/event_hash И payload-указатели (input_head_hash, journal_head_hash, final_state_hash,
state_*_hash) при разных run_id расходятся ЗАКОНОМЕРНО — не недетерминизм, а другая метка прогона.
Плюс фактическое recorded_at_ns физически недетерминировано. Побитовое сравнение путало это с реальным
недетерминизмом.

## Решение
**Разные run_id + общий replay_group_id.** Два прогона одной replay-пары — самостоятельные исполнения со
своей идентичностью (свой run_id, своя §5-цепь), связанные replay_group_id. Не схлопываем в одну причинную
цепочку.

**Нормализованное сравнение.** replay-детерминизм проверяется по НОРМАЛИЗОВАННОЙ проекции события, не по
сырому event_hash:
- ИСКЛЮЧАЕМ (идентификаторы экземпляра + физическое время): `run_id`, `prev_hash`, `event_hash`,
  фактическое `recorded_at_ns`.
- ВКЛЮЧАЕМ (детерминированный контент): весь payload, `logical_recorded_at_ns`, `seq`, `event_time_ns`,
  `received_ts_ns`, `event_type`, `caused_by`, `schema_version`.
- Транзитивные payload-хэши (input_head_hash/journal_head_hash/…) — часть нормализованного контента и
  ДОЛЖНЫ совпадать по значению, потому что при исключённом run_id они вычисляются от нормализованной же
  проекции своих референтов (детерминированы от входа, не от метки прогона).
- `normalized_replay_hash` = sha256 канонизированной нормализованной проекции. `normalization_version`
  версионирует само правило нормализации.

**Предусловия сравнимости** (иначе это НЕ replay-пара, а разные эксперименты — mismatched-input):
совпадают `rng_seed`, `config_hash`, `code_hash`, входной `dataset_hash`, `schema_version`.

**Время.** `logical_recorded_at_ns` (детерминированное «когда записалось бы при идеальном replay») — в
каноническом payload, входит в нормализацию. Фактическое `recorded_at_ns` — вне нормализации (в §5-цепи для
целостности остаётся).

## input_attached — обязательный payload
`dataset_hash` (хэш канонизированного СОДЕРЖИМОГО, не файла), `dataset_schema_version`, `source`
(тип/идентификатор без секретов и локальных путей), `event_count`, `first_received_ts_ns`,
`last_received_ts_ns`, `canonicalization_version`, `ordering_rule` (напр. `received_ts_ns,event_id`),
`dedup_rule`. UTC+ns фиксированы схемой. Пустой вход: диапазоны `null`, `event_count=0`, хэш от
канонического пустого набора. exchange_ts-диапазон — только информационно; причинная граница и сортировка
ВСЕГДА по `received_ts_ns`.

## Manifest — ОТДЕЛЬНЫЙ артефакт, аудитор ОБЯЗАН проверять
Не декоративная квитанция. Канонический JSON, НЕ включается в журнал (иначе циклическая зависимость);
собственный `manifest_hash` публикуется рядом. Поля:
- `replay_group_id`;
- `runs[]`: `{run_id, journal_head_hash, event_count}`;
- общие `dataset_hash`, `config_hash`, `code_hash`, `rng_seed`, `schema_version`;
- `normalization_version`;
- `normalized_replay_hash` каждого прогона;
- `replay_equal` (итог).

Аудитор сверяет: (1) каждый journal_head_hash/event_count манифеста = фактический из своего JSONL;
(2) общие параметры реально совпадают у всех прогонов группы; (3) normalized_replay_hash пересчитан
аудитором независимо совпадает с заявленным; (4) `replay_equal` истинно ⟺ все normalized_replay_hash
группы равны. Расхождение нормализованных хэшей при совпавших предусловиях = нарушение §7 (недетерминизм).

Подпись/аутентичность — ОТЛОЖЕНО: сначала детерминизм и цепочка, вопрос подписи отдельным DR.

## Порядок (контракт до кода)
Формулировка сверяется обеими сторонами ДО кода. После сверки: praxis пишет baseline по этому формату,
Arête дорабатывает `audit/audit.py` (`replay_compare` → нормализованное сравнение + чтение/проверка
manifest). Первый прогон: `run_started → input_attached → run_finished` (пусто) + парный replay + manifest.
