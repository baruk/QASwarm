---
name: test-design
description: Проектирование риск-ориентированной тестовой модели в Qase на основании проверенного Requirement Review.
---

## Purpose

Skill проектирует риск-ориентированную тестовую модель и формирует Test Model Artifact — спецификацию создания, пригодную для исполнения паблишером. Сущности в Qase Skill не создаёт и тестирование не выполняет.

## Inputs

Обязательно: валидный Requirement Review Artifact. Опционально: ограничения тестирования, проектные правила приоритизации, сведения о существующих кейсах, полученные агентом read-only.

## Procedure

1. Определи тестируемые функции и бизнес-правила из Review.
2. Оцени риски и бизнес-критичность.
3. Определи Positive и Negative Scenarios.
4. Покрой Validation Rules, значимые Boundaries, альтернативные потоки и зависимости.
5. Используй risk-based подход для дополнительного покрытия, помечая такие проверки как QA/Risk-based.
6. Для каждого сценария сформулируй проверяемый Expected Result и назначь Priority по риску.
7. Сохрани Traceability к Source Requirement.
8. Сравни с переданными существующими кейсами и исключи необоснованные дубли.
9. Присвой Local ID: сюиты `S-<n>`, кейсы `C-<n>`, shared steps `SS-<n>`; проставь `Ready to create: yes|no` с причиной для `no`.
10. Сформируй artifact по структуре из Output.

## Rules

- Не придумывай бизнес-поведение, которого нет в Requirement Review.
- Risk-based проверки допустимы, если явно обозначены как QA/Risk-based, а не как бизнес-требование.
- Не создавай сценарии, проверяющие одно и то же без дополнительной ценности.
- Нерешённые Ambiguities и Conflicts не скрывай: зависимый сценарий получает `Ready to create: no` и причину.
- Только read-only Qase. Никаких upsert, create, delete, записи результатов, изменения прогонов.
- Спецификация не содержит ID созданных сущностей и следов создания — факт создания фиксирует паблишер.
- Priority: `HIGH | MEDIUM | LOW`, если проектом не определён другой контракт; Type: `positive | negative | boundary | risk-based`. Если контракт проекта иной — укажи применённый контракт в шапке artifact.

## Output

Artifact `artifacts/test-model/v<N>_<YYMMDD_HHMM>.md`: шапка из AGENTS.md §6, применённый контракт Priority/Type, дерево сюит (Local ID, название, родитель) или явное указание, что сюиты не нужны, shared steps с Local ID, затем по каждому сценарию:

- Local ID (`C-<n>`)
- `Ready to create: yes|no`
- Suite (Local ID)
- Title
- Source Requirement
- Priority
- Type
- Preconditions, если применимо
- Steps или Test Intent
- Expected Result
- Risk Reference, если сценарий risk-based
- Open Question или Blocker, если сценарий зависит от нерешённой неоднозначности

В конце: покрытие требований, список непокрытых требований с причиной и список `Ready to create: no` с причинами.

## Completion Criteria

- критические подтверждённые требования имеют покрытие;
- Positive, Negative Scenarios, Validation Rules и значимые Boundaries покрыты;
- Expected Result определён для каждого готового сценария;
- Priority назначен либо явно помечен как требующий внешнего решения;
- Traceability сохранена; risk-based сценарии отделены от бизнес-требований;
- необоснованные дубликаты отсутствуют, проверка дублей отражена;
- нерешённые Ambiguities и Blockers видимы;
- у каждого сценария есть `Ready to create`, у каждого `no` — причина;
- спецификация не содержит ID Qase и следов создания;
- artifact готов к передаче `qase-publisher` без дополнительных решений.