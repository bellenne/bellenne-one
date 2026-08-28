# BellenneVector Product Design Specification

## 1. Product

Official name:

BellenneVector

Short module name:

Vector

Product category:

Marketplace Promotion & Advertising Analytics

BellenneVector предназначен для анализа продвижения товаров на маркетплейсах.

Основные задачи продукта:

* анализ рекламных кампаний;
* анализ эффективности продвижения;
* контроль рекламных расходов;
* анализ поисковых позиций;
* анализ рекламных позиций;
* анализ ключевых запросов;
* контроль CTR, CPC, CPM, CR и других рекламных метрик;
* анализ заказов и продаж из рекламы;
* оценка ДРР;
* сравнение рекламных расходов с полученной выручкой;
* анализ эффективности отдельных товаров;
* анализ эффективности отдельных кампаний;
* анализ динамики продвижения;
* выявление неэффективных расходов;
* поиск точек роста;
* сравнение периодов;
* построение рекомендаций на основе данных.

---

# 2. Design System

BellenneVector НЕ имеет собственной дизайн-системы.

Обязательный источник:

`docs/BRANDBOOK.md`

Все интерфейсы Vector должны использовать общую Bellenne Design System.

BellenneVector должен восприниматься как часть того же приложения, что:

BellennePulse

BellenneEcho

и будущие продукты Bellenne.

---

# 3. Product Identity

Основная идея Vector:

Direction.
Growth.
Efficiency.
Movement.

Название Vector отражает направление продвижения бизнеса.

Интерфейс должен помогать пользователю быстро отвечать на вопросы:

Куда уходят рекламные деньги?

Какие кампании работают?

Какие товары реально растут благодаря продвижению?

Где эффективность ухудшается?

Какие действия необходимо предпринять?

Vector является аналитическим инструментом.

Data first.

Decoration second.

---

# 4. Product Accent

Vector Accent:

#2563EB

Token:

```css
--product-accent: #2563EB;
```

Название:

Vector Blue

Vector Blue является идентификационным цветом модуля.

Он используется для:

* иконки Vector;
* active Vector navigation;
* выбранных кампаний;
* selected states;
* focus states;
* основного ряда данных Vector;
* primary promotion charts;
* небольших highlights;
* product-specific links;
* отдельных primary actions.

Vector Blue НЕ является универсальным цветом положительного результата.

---

# 5. Semantic Colors

Product Accent и semantic colors должны использоваться независимо.

Positive:

#22D3A5

Negative:

#FF5C70

Warning:

#F59E0B

Neutral:

#AAB3C2

Vector Accent:

#2563EB

Пример:

рост CTR является положительным изменением и отображается Positive color.

Тот факт, что CTR находится внутри Vector, не означает, что показатель должен быть синим.

---

# 6. Core Product Entities

Основные бизнес-сущности Vector:

Campaign

Advert

Product

Marketplace

Search Query

Keyword

Placement

Position

Advertising Cost

Order

Sale

Promotion Metric

Budget

Bid

Period

Recommendation

---

# 7. Campaign

Campaign является одной из основных сущностей Vector.

Интерфейс кампании должен позволять быстро увидеть:

Campaign Name

Status

Marketplace

Products

Budget

Spend

Impressions

Clicks

CTR

CPC

Orders

Revenue

CR

DRR

ROAS

Current performance

Performance dynamics

Не перегружать список кампаний второстепенной информацией.

Основные показатели должны быть доступны для быстрого сравнения.

---

# 8. Campaign Status

Предпочтительные статусы:

Active

Paused

Stopped

Budget Limited

Completed

Error

Unknown

Статусы должны использовать semantic colors.

Active:

Positive

Paused:

Neutral

Budget Limited:

Warning

Error:

Negative

Не использовать Vector Blue для отображения статуса Active.

---

# 9. Advertising Metrics

Vector должен использовать единообразные названия и форматирование рекламных метрик.

Основные метрики:

Spend

Impressions

Clicks

CTR

CPM

CPC

Orders

Conversion Rate

Cost Per Order

Revenue

DRR

ROAS

Budget

Bid

Position

Если интерфейс русскоязычный, допустимы:

Расход

Показы

Клики

CTR

CPM

CPC

Заказы

CR

CPO

Выручка

ДРР

ROAS

Бюджет

Ставка

Позиция

Не использовать разные названия одной метрики на разных страницах.

---

# 10. Financial Formatting

Финансовые показатели должны иметь единый формат.

Примеры:

125 430 ₽

1 250 000 ₽

15,4%

2,7x

Все числа должны использовать:

```css
font-variant-numeric: tabular-nums;
```

При отображении больших чисел допускаются сокращённые KPI:

1.25M

245K

но таблицы предпочтительно должны показывать точные значения.

---

# 11. KPI Cards

Vector использует общий Bellenne MetricCard / KPICard.

Типичный набор KPI для Overview:

Advertising Spend

Revenue from Promotion

Orders from Promotion

DRR

CTR

CPC

CR

ROAS

Главное значение:

крупное.

Дополнительная информация:

comparison with previous period;

comparison with plan;

percentage dynamics.

Не создавать уникальный card style специально для Vector.

---

# 12. Performance Semantics

Необходимо учитывать, что направление "лучше / хуже" зависит от метрики.

Например:

Revenue ↑ = Positive

Orders ↑ = Positive

CTR ↑ = Positive

CR ↑ = Positive

ROAS ↑ = Positive

CPC ↓ = Positive

CPO ↓ = Positive

DRR ↓ = Positive

Advertising Spend ↑ не является автоматически Positive или Negative.

Для неоднозначных изменений использовать Neutral до тех пор, пока значение нельзя оценить относительно цели или результата.

Не определять semantic color только по направлению стрелки.

---

# 13. DRR

ДРР является одной из ключевых метрик Vector.

Отображать:

Current DRR

Previous DRR

Target DRR при наличии

Change

Пример:

DRR

14.8%

Target 16%

↓ 1.7 pp

В таком случае уменьшение ДРР может считаться Positive.

Не использовать красный только потому, что значение уменьшилось.

---

# 14. ROAS

ROAS является показателем эффективности рекламы.

Пример:

ROAS

6.4x

Previous 5.8x

↑ 10.3%

Использовать единое форматирование во всех разделах Vector.

---

# 15. Search Positions

Search position должна быть визуально простой для сравнения.

Пример:

Current Position

8

Previous

14

↑ +6 positions

Улучшение позиции означает уменьшение её числового значения.

Логика semantic state должна учитывать это.

Position 5 лучше Position 20.

Не определять качество изменения исключительно математическим ростом числа.

---

# 16. Search Query Analytics

Для поисковых запросов пользователь должен быстро видеть:

Query

Current Position

Previous Position

Position Change

Impressions

Clicks

CTR

Orders

Conversion

Revenue

Advertising Spend

DRR

Не создавать крупные отдельные карточки для каждого поискового запроса.

Использовать плотную DataTable.

---

# 17. Product Promotion Analytics

Для каждого товара должны быть доступны:

Product

Marketplace

Advertising Spend

Impressions

Clicks

CTR

CPC

Orders

Revenue

CR

DRR

ROAS

Position

Dynamics

Интерфейс должен позволять быстро сравнивать товары между собой.

---

# 18. Campaign List

Основной список кампаний должен быть data-dense.

Предпочтительно использовать:

DataTable

или

compact table/list hybrid.

Основные колонки:

Campaign

Status

Products

Budget

Spend

Impressions

Clicks

CTR

CPC

Orders

Revenue

DRR

ROAS

Dynamics

Не создавать огромные campaign cards, если пользователь работает одновременно с большим количеством кампаний.

---

# 19. Filters

Vector является аналитическим продуктом, поэтому FilterBar является важным компонентом.

Типичные фильтры:

Period

Marketplace

Campaign

Campaign Status

Product

Category

Search Query

Promotion Type

Warehouse при необходимости

Filters должны использовать стандартный Bellenne FilterBar.

Не создавать отдельную систему фильтров для Vector.

---

# 20. Date Comparison

Vector должен поддерживать сравнение периодов.

Типичные варианты:

Today

Yesterday

7 days

30 days

Current period vs previous period

Custom period

UI сравнения периода должен быть совместим с BellennePulse.

Не создавать Vector-specific date picker.

---

# 21. Charts

Vector использует общую Bellenne chart system.

Основная серия Vector:

#2563EB

Secondary Bellenne colors могут использоваться для дополнительных series.

Основные типы графиков:

Line Chart

Area Chart

Bar Chart

Stacked Bar Chart

Donut Chart — только если он действительно помогает сравнению долей.

Не использовать декоративные chart types.

---

# 22. Plan vs Fact

Если используется план:

Plan:

muted gray;

dashed line;

lower visual priority.

Fact:

Vector Accent или metric-specific color;

solid line;

higher visual priority.

Этот паттерн должен совпадать с BellennePulse.

---

# 23. Spend vs Revenue

При сравнении рекламных расходов и выручки нельзя использовать semantic Positive/Negative colors как обычные series colors.

Предпочтительно:

Revenue:

Bellenne Violet или соответствующий общий Revenue token.

Advertising Spend:

Vector Blue.

Semantic colors использовать только для оценки результата.

---

# 24. Efficiency

Vector должен визуально отделять:

Amount metrics

от

Efficiency metrics.

Amount:

Spend

Revenue

Orders

Clicks

Impressions

Efficiency:

CTR

CPC

CR

CPO

DRR

ROAS

Это логическое разделение, а не отдельный визуальный стиль.

---

# 25. Recommendations

Vector может отображать аналитические рекомендации.

Примеры:

High CPC with low conversion

DRR above target

Position is declining

Campaign budget is exhausted

High CTR but low CR

Product has growth potential

Рекомендации должны выглядеть как стандартный Bellenne insight component.

Не создавать отдельный "AI design".

---

# 26. Recommendation Severity

Тип рекомендации:

Opportunity

Attention

Critical

Information

Использовать semantic system.

Opportunity:

Positive

Attention:

Warning

Critical:

Negative

Information:

Vector Accent или neutral information style.

---

# 27. AI Analytics

Если Vector использует AI для анализа или рекомендаций:

AI является функцией.

AI НЕ является отдельным брендом.

Запрещено создавать:

* rainbow AI gradients;
* glowing AI cards;
* magical backgrounds;
* отдельную AI typography;
* отдельную AI component library.

Использовать стандартные Bellenne components.

---

# 28. Bid Analytics

При работе со ставками необходимо отображать:

Current Bid

Previous Bid

Recommended Bid при наличии

Position

CPC

Result

Не считать увеличение ставки автоматически положительным.

Semantic state должен определяться эффективностью результата.

---

# 29. Budget

Для бюджета отображать:

Daily Budget

Spent

Remaining

Usage percentage

Forecast при наличии

Progress bar допустим.

При приближении к лимиту:

Warning.

При исчерпании:

Negative или Warning в зависимости от контекста.

---

# 30. Promotion Overview

Главная страница Vector должна отвечать минимум на следующие вопросы:

Сколько было потрачено?

Сколько получено выручки?

Сколько получено заказов?

Какой текущий ДРР?

Какой ROAS?

Как изменилась эффективность?

Какие кампании работают лучше всего?

Какие кампании требуют внимания?

Какие товары получают результат от продвижения?

---

# 31. Recommended Overview Structure

Предпочтительная композиция:

Page Header

Filter Bar

Main KPI row

Spend / Revenue dynamics

Efficiency metrics

Campaign performance

Product performance

Search position dynamics

Insights / Problems

Структура может меняться под задачу.

Не менять при этом общую Bellenne visual system.

---

# 32. Alerts

Типичные Vector alerts:

Budget exhausted

DRR above target

CPC spike

Conversion drop

Position drop

Campaign stopped

No sales

Unexpected spend growth

Использовать общую Bellenne alert system.

---

# 33. Tables

Vector является data-heavy продуктом.

Таблицы являются основным инструментом анализа.

Обязательно:

text left aligned;

numbers right aligned;

tabular numerals;

sticky header для больших таблиц;

sorting;

filtering;

clear column hierarchy;

compact row height.

Не использовать чрезмерное количество цветных backgrounds внутри таблиц.

---

# 34. Conditional Formatting

Conditional formatting должно использоваться умеренно.

Хорошо:

↑ 14%

↓ 8%

DRR 26%

Position ↑ 5

Плохо:

каждая numeric cell имеет собственный яркий background.

Цвет должен помогать анализировать данные, а не создавать визуальный шум.

---

# 35. Product Navigation

Внутри Vector предпочтительны разделы:

Overview

Campaigns

Products

Search Queries

Positions

Analytics

Recommendations

Settings

Это внутренняя структура Vector.

Она не должна заменять Global Bellenne navigation.

---

# 36. Unified Bellenne Navigation

После объединения продуктов ожидаемая структура:

Bellenne

Overview

Pulse

Echo

Vector

Settings

При входе в Vector пользователь должен оставаться внутри общего Bellenne Shell.

---

# 37. Product Transition

Переход:

Pulse → Vector

не должен ощущаться как переход на другой сайт.

Не меняются:

Sidebar geometry

Header geometry

Typography

Cards

Buttons

Inputs

Tables

Modals

Dropdowns

Spacing

Radius

Shadows

Меняется:

Product context

Product navigation

Product Accent

Business data

---

# 38. Shared Components

Vector должен использовать общие компоненты Bellenne.

Например:

Button

Card

MetricCard

DataTable

FilterBar

Select

DateRangePicker

Modal

Tabs

Badge

Tooltip

Dropdown

ChartContainer

Alert

EmptyState

Не создавать:

VectorButton

VectorTable

VectorModal

если отличие заключается только в цвете.

---

# 39. Vector-specific Components

Product-specific компоненты допустимы, если они описывают бизнес-сущность.

Хорошо:

CampaignTable

CampaignStatus

PromotionMetricCard

SearchPositionTable

QueryPerformanceTable

BudgetUsage

BidHistory

PromotionInsight

ProductPromotionTable

Плохо:

VectorBlueButton

VectorCard

VectorInput

---

# 40. Marketplace Independence

Несмотря на то, что текущая версия Vector ориентирована на Wildberries, UI architecture не должна быть искусственно связана только с WB, если это не требуется бизнес-логикой.

Избегать названий общих компонентов:

WBButton

WBChart

WBTable

Предпочитать:

CampaignTable

MarketplaceFilter

PromotionMetrics

Однако WB-specific API adapters и business logic могут иметь соответствующие названия.

---

# 41. Wildberries Branding

Bellenne является главным визуальным брендом приложения.

Не копировать Wildberries visual identity.

Не использовать фирменный magenta Wildberries как основной UI accent.

Wildberries может отображаться:

как marketplace name;

marketplace icon;

source badge;

filter value.

Но приложение должно оставаться визуально Bellenne.

---

# 42. Loading States

При загрузке аналитики использовать стандартные Bellenne loading states.

Предпочтительно:

skeleton;

compact spinner;

loading state внутри chart/table.

Не заменять весь экран большой loading animation.

---

# 43. Empty States

Empty states должны быть функциональными.

Пример:

No campaigns found

Измените фильтры или синхронизируйте рекламные кампании.

Не использовать огромные иллюстрации.

---

# 44. Error States

API errors должны быть понятны пользователю.

Например:

Не удалось получить статистику рекламных кампаний.

Повторить

При возможности показывать:

source;

period;

last successful sync.

Не показывать пользователю raw API exceptions.

---

# 45. Data Freshness

Так как рекламная статистика может обновляться с задержкой, интерфейс должен уметь отображать:

Last Updated

Syncing

Delayed Data

Data Error

Это information state.

Не путать задержку API со статусом рекламной кампании.

---

# 46. Responsive

Desktop является основным интерфейсом Vector.

На небольших экранах:

KPI grid перестраивается;

charts переходят в одну колонку;

filters wrap;

tables получают horizontal scroll;

sidebar работает согласно общей Bellenne responsive system.

Не превращать аналитические таблицы автоматически в огромные cards.

---

# 47. Future Unified Overview

После объединения продуктов данные Vector могут использоваться на общем Bellenne Overview.

Например:

Advertising Spend

DRR

ROAS

Campaign Alerts

Promotion Efficiency

Top Growth Opportunity

Поэтому аналитические компоненты желательно проектировать достаточно универсально для повторного использования.

---

# 48. Relationship with BellennePulse

Pulse отвечает преимущественно на вопрос:

Что происходит с бизнесом?

Vector отвечает:

Почему продвижение даёт такой результат и насколько эффективно расходуется рекламный бюджет?

Данные могут пересекаться.

Например:

Revenue

Orders

DRR

Но контекст отличается.

Не создавать дублирующую бизнес-логику там, где её можно использовать совместно после объединения.

---

# 49. Relationship with BellenneEcho

Echo отвечает за клиентскую коммуникацию и репутацию.

Vector отвечает за привлечение и продвижение.

Они используют одну Bellenne Design System, но разные business entities.

Не переносить review-specific patterns в Vector без бизнес-причины.

---

# 50. Future Unified Architecture

Целевая архитектура интерфейса:

Bellenne Shell
→ Product
→ Section
→ Page

Для Vector:

Bellenne Shell
→ Vector
→ Campaigns
→ Campaign Details

или:

Bellenne Shell
→ Vector
→ Analytics
→ Search Queries

Не создавать:

Vector Standalone Shell
→ Vector Navigation
→ Page

если это можно избежать.

---

# 51. Main UX Principle

Vector должен помогать пользователю находить:

Signal

Cause

Impact

Action

Интерфейс не должен просто показывать сотни рекламных цифр.

Visual hierarchy должна показывать:

что произошло;

насколько это важно;

где проблема;

где возможность;

что стоит проверить дальше.

---

# 52. No Visual Drift

BellenneVector не должен превращаться в отдельный визуальный продукт.

Запрещено создавать специально для Vector:

новый UI kit;

новую typography;

новые radii;

отдельную sidebar;

новую card system;

собственные shadows;

отдельную form system;

новую chart palette;

новый visual language.

Vector Blue является акцентом.

Он не является новой темой.

---

# 53. Definition of Done

Перед завершением любой Vector UI-задачи проверить:

использован общий Bellenne Design System;

использован `docs/products/VECTOR.md`;

Product Accent применяется только там, где он нужен;

semantic colors имеют правильный смысл;

не появилось arbitrary colors;

не появилось arbitrary spacing;

не создан новый visual language;

использованы существующие Bellenne components;

числовые показатели легко сравниваются;

таблицы сохраняют высокую плотность данных;

эффективность метрик правильно интерпретируется;

DRR/CPC/CPO не получают неверный semantic state;

позиции правильно интерпретируют направление улучшения;

нет незапрошенного redesign;

страница совместима с будущим Bellenne Shell.

---

# 54. Final Principle

BellenneVector должен ощущаться как:

BellennePulse для продвижения.

Пользователь должен перейти:

Pulse → Echo → Vector

и видеть одну систему.

Different purpose.

Same product family.

Same design language.

One Bellenne ecosystem.
