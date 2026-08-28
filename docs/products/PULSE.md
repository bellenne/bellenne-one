# BellennePulse Product Design Specification

## 1. Product

Official name:

BellennePulse

Short module name:

Pulse

Product category:

Marketplace Business Intelligence & Performance Analytics

BellennePulse предназначен для общей аналитики бизнеса на маркетплейсах.

Основные задачи продукта:

* ежедневная бизнес-аналитика;
* контроль выручки;
* контроль заказов;
* план / факт;
* динамика показателей;
* контроль выкупа;
* анализ ДРР;
* анализ выполнения планов;
* сравнение периодов;
* контроль эффективности бизнеса;
* сводная аналитика по маркетплейсам;
* сводная аналитика по товарам и направлениям;
* выявление отклонений;
* предоставление общей картины состояния бизнеса.

---

# 2. Special Status

BellennePulse является исходной и уже существующей визуальной реализацией экосистемы Bellenne.

Текущий интерфейс BellennePulse является REFERENCE IMPLEMENTATION.

Это означает:

* существующий дизайн Pulse не должен автоматически перерабатываться;
* существующие страницы Pulse не должны приводиться к новым правилам только ради формального соответствия документации;
* существующие компоненты Pulse не должны визуально изменяться без прямого запроса пользователя;
* существующий layout Pulse не должен заменяться;
* существующий sidebar Pulse не должен перерабатываться;
* существующая типографика не должна массово заменяться;
* существующие spacing, cards, tables, charts и controls не должны автоматически мигрировать.

Главное правило:

DO NOT REDESIGN EXISTING BELLENNEPULSE UI WITHOUT AN EXPLICIT USER REQUEST.

---

# 3. Role in Bellenne Ecosystem

BellennePulse является визуальным ориентиром для:

BellenneEcho

BellenneVector

будущих продуктов Bellenne.

При объединении приложений:

Echo и Vector должны адаптироваться к общей системе Bellenne и визуальному языку Pulse.

Pulse не должен адаптироваться под Echo или Vector.

Направление унификации:

Echo → Bellenne

Vector → Bellenne

Pulse → reference

а не:

Pulse → redesign

---

# 4. Relationship with BRANDBOOK

Общий документ:

`docs/BRANDBOOK.md`

описывает Bellenne Design System.

Для НОВЫХ компонентов и НОВЫХ страниц Pulse необходимо использовать:

1. существующие компоненты Pulse;
2. существующие визуальные паттерны Pulse;
3. общий Bellenne BRANDBOOK.

Если существующая реализация Pulse визуально отличается от значения, описанного в BRANDBOOK:

НЕ изменять существующую реализацию автоматически.

Существующий интерфейс считается допустимым legacy/reference implementation.

---

# 5. Existing Implementation Priority

При работе внутри существующей страницы Pulse:

сначала изучить соседние компоненты.

Новый элемент должен визуально соответствовать существующей странице.

Не следует менять окружающий интерфейс ради того, чтобы новый компонент идеально соответствовал абстрактному design token.

Consistency with existing Pulse UI имеет приоритет для существующих страниц.

---

# 6. New Pulse Pages

Для полностью новых страниц BellennePulse:

использовать общий Bellenne Design System.

При этом предпочтительно повторно использовать существующие Pulse components.

Порядок:

Existing Pulse Component

→ Existing Pulse Pattern

→ Shared Bellenne Component

→ BRANDBOOK Pattern

→ Minimal new implementation

Не создавать новый визуальный язык.

---

# 7. Product Accent

Pulse Product Accent:

#7B3CFF

Название:

Pulse Violet

Token:

```css
--product-accent: #7B3CFF;
```

Этот цвет используется как идентификатор модуля Pulse.

---

# 8. Product Identity

Основная идея Pulse:

Business Pulse.

Pulse показывает пользователю текущее состояние бизнеса.

Основные смысловые категории:

Performance

Dynamics

Plan

Fact

Growth

Decline

Business Health

Trend

Control

---

# 9. Main Questions

Pulse должен помогать быстро отвечать:

Как идут продажи?

Выполняется ли план?

Как изменилась выручка?

Как изменились заказы?

Что происходит с выкупом?

Какой текущий ДРР?

Какие показатели растут?

Какие показатели ухудшаются?

Где есть отклонение от плана?

Что изменилось относительно предыдущего периода?

---

# 10. Core Entities

Основные сущности:

Marketplace

Period

Revenue

Orders

Buyout

Buyout Rate

Average Order Value

DRR

Plan

Fact

Performance

Dynamics

Product

Category

Warehouse

Business Metric

---

# 11. Main Metrics

Типичные Pulse KPI:

Revenue

Orders

Average Order Value

Buyout Rate

DRR

Plan Completion

Revenue Dynamics

Orders Dynamics

Buyout Dynamics

---

# 12. Plan vs Fact

Plan vs Fact является одним из основных аналитических паттернов Pulse.

Plan должен иметь меньший визуальный приоритет.

Fact является основным значением.

При отображении динамики:

Fact — основная серия.

Plan — secondary / muted series.

---

# 13. KPI Cards

Использовать существующий Pulse KPI pattern.

Не создавать новый внешний вид KPI cards, если существующий компонент уже существует.

Типичная структура:

Metric Label

Current Value

Plan / Previous Value

Change

Optional Sparkline

---

# 14. Data Tables

Pulse является data-heavy продуктом.

Таблицы являются одним из основных интерфейсов.

При добавлении колонок или функций:

сохранять существующую структуру таблицы.

Не выполнять redesign таблицы в рамках обычной функциональной задачи.

---

# 15. Charts

Существующие chart styles BellennePulse должны сохраняться.

При создании нового графика:

сначала найти аналогичный существующий график Pulse.

Повторно использовать:

colors;

grid;

tooltip;

legend;

axis formatting;

container;

spacing.

Не создавать новую chart aesthetic.

---

# 16. Filters

Новые фильтры должны использовать существующий Pulse filter pattern.

Не создавать отдельный FilterBar style, если аналог уже существует.

---

# 17. Navigation

Существующая навигация Pulse считается reference implementation.

Не изменять:

sidebar structure;

menu geometry;

spacing;

icons;

active state;

header

без прямой задачи пользователя.

При будущем объединении допускается перенос страниц Pulse внутрь общего Bellenne Shell.

Такой перенос должен сохранять визуальные и функциональные свойства существующего Pulse максимально близко к текущему состоянию.

---

# 18. Existing Components

Существующие Pulse components следует рассматривать как кандидатов для превращения в общие Bellenne components.

Например:

Pulse Card

может позднее стать:

Bellenne Card

Но визуальное изменение компонента не должно быть обязательным условием такой миграции.

Предпочтительно:

extract / reuse

вместо:

rewrite / redesign.

---

# 19. Shared Component Extraction

Во время объединения приложений допускается технически выносить существующий Pulse component в общий слой.

Например:

`pulse/components/Card`

→

`shared/components/Card`

при условии, что:

* внешний вид не изменился;
* поведение не изменилось;
* public API компонента не ломается без необходимости;
* изменение не вызывает ненужный visual diff.

---

# 20. No Opportunistic Refactoring

Во время работы над Echo или Vector запрещено одновременно использовать задачу слияния как повод для массового изменения Pulse.

Не выполнять:

"заодно обновил Pulse"

"заодно унифицировал цвета"

"заодно поменял карточки"

"заодно заменил sidebar"

"заодно обновил charts"

если пользователь этого явно не просил.

---

# 21. Pulse as Baseline

Когда возникает вопрос:

"Как должен выглядеть этот элемент в Echo или Vector?"

необходимо сначала проверить:

"Есть ли аналог в Pulse?"

Если существует аналогичный компонент Pulse:

его визуальный язык должен иметь высокий приоритет.

---

# 22. Product Navigation

Типичные разделы Pulse могут включать:

Overview

Analytics

Performance

Products

Marketplaces

Reports

Settings

Фактическая существующая структура приложения имеет приоритет над этим списком.

Не перестраивать навигацию под этот документ.

---

# 23. Relationship with Echo

Pulse отвечает:

Что происходит с бизнесом?

Echo отвечает:

Что происходит с отзывами и коммуникацией с клиентами?

Echo должен использовать дизайн-систему Bellenne, основанную на визуальном языке Pulse.

Pulse не должен адаптироваться под Echo.

---

# 24. Relationship with Vector

Pulse отвечает:

Какой результат показывает бизнес?

Vector отвечает:

Как продвижение влияет на результат и насколько эффективно расходуется рекламный бюджет?

Общие метрики могут включать:

Revenue

Orders

DRR

Но контекст отображения может отличаться.

Не менять существующую аналитику Pulse только из-за появления аналогичной метрики в Vector.

---

# 25. Future Unified Application

Целевая архитектура:

Bellenne Shell
→ Pulse
→ Page

Bellenne Shell
→ Echo
→ Page

Bellenne Shell
→ Vector
→ Page

При интеграции Pulse:

предпочитать wrapping / extraction / reuse.

Избегать полного переписывания существующего UI.

---

# 26. Migration Principle

При слиянии приложений использовать принцип:

Preserve Pulse.

Adapt Echo.

Adapt Vector.

Extract shared components when useful.

Do not redesign for the sake of architectural purity.

---

# 27. Functional Changes

Если пользователь просит изменить функциональность Pulse:

изменение разрешено.

Например:

* добавить фильтр;
* добавить KPI;
* изменить формулу;
* добавить таблицу;
* добавить страницу;
* исправить ошибку.

Но функциональная задача не является разрешением на визуальный redesign соседних элементов.

---

# 28. Explicit Redesign Rule

Visual redesign Pulse разрешён только если пользователь явно использует запросы вроде:

"измени дизайн Pulse"

"обнови интерфейс Pulse"

"приведи эту страницу к новому дизайну"

"переделай sidebar"

"измени внешний вид карточек"

"унифицируй старые страницы Pulse"

Без такого запроса существующий дизайн должен сохраняться.

---

# 29. Definition of Done

Перед завершением задачи внутри Pulse проверить:

* существующий UI не изменился без необходимости;
* отсутствует незапрошенный redesign;
* новый элемент визуально совместим с окружающим Pulse UI;
* переиспользованы существующие components;
* не создан новый visual language;
* функциональность существующих страниц не сломана;
* responsive behavior не ухудшен;
* shared component extraction не вызвал visual regression.

---

# 30. Final Principle

BellennePulse is the reference implementation.

Preserve what already works.

Extend, do not redesign.

Reuse, do not reinvent.

Echo and Vector should converge toward BellennePulse.

BellennePulse should not be rebuilt just to satisfy the merger.
