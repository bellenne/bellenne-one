# BellenneNest Product Design Specification

## 1. Product

Official name:

BellenneNest

Short module name:

Nest

Product category:

Layout Optimization Engine

BellenneNest — вычислительный сервис экосистемы Bellenne для поиска оптимальной раскладки элементов при производстве обоев и других совместимых печатных материалов.

Основная работа продукта выполняется через API.

Пользовательский интерфейс является административным и конфигурационным слоем над Nest Engine.

Основные задачи:

* управление настройками алгоритма;
* управление правилами раскладки;
* управление ограничениями;
* создание и редактирование пресетов;
* настройка параметров материалов;
* настройка параметров производства;
* управление API;
* просмотр состояния сервиса;
* тестирование расчётов;
* просмотр результатов расчёта при необходимости;
* диагностика ошибок;
* контроль версий алгоритма и конфигурации при необходимости.

---

# 2. Design System

BellenneNest НЕ имеет собственной дизайн-системы.

Обязательный источник:

`docs/BRANDBOOK.md`

Все интерфейсы Nest должны использовать общую Bellenne Design System.

Nest должен визуально восприниматься как часть той же платформы, что:

BellennePulse

BellenneEcho

BellenneVector

---

# 3. Product Role

Nest отличается от Pulse, Echo и Vector по характеру продукта.

Pulse является аналитическим продуктом.

Echo является операционным продуктом для работы с отзывами.

Vector является аналитическим продуктом для продвижения.

Nest является:

Computational Service

Optimization Engine

Infrastructure Module

Configuration Tool

Поэтому Nest НЕ обязан копировать dashboard structure BellennePulse.

Он использует тот же визуальный язык, но другую информационную архитектуру.

---

# 4. Product Identity

Основные идеи Nest:

Optimization.

Placement.

Efficiency.

Precision.

Constraints.

Computation.

Название Nest связано с задачей nesting:

поиском эффективного размещения элементов внутри доступного пространства или материала.

Nest должен восприниматься как точный инженерный инструмент.

Не как creative editor.

Не как CAD.

Не как графический редактор.

Не как полноценная производственная MES.

---

# 5. Product Accent

Nest Product Accent:

#A855F7

Название:

Nest Violet

Token:

```css
--product-accent: #A855F7;
```

Этот цвет уже является частью общей Bellenne palette.

Nest Accent используется для:

* Nest product icon;
* active Nest navigation;
* selected settings;
* active configuration section;
* selected preset;
* focus state;
* layout visualization accent;
* API-related highlights;
* основного action внутри Nest;
* небольших product-specific indicators.

Nest Accent НЕ является semantic status color.

---

# 6. Semantic Colors

Semantic colors остаются общими для всей платформы.

Success:

#22D3A5

Danger:

#FF5C70

Warning:

#F59E0B

Neutral:

#AAB3C2

Nest Accent:

#A855F7

Примеры:

API доступен → Success

Ошибка расчёта → Danger

Некорректная конфигурация → Warning

Выбранный preset → Nest Accent

Не использовать Nest Violet для обозначения Success.

---

# 7. Primary Product Principle

Nest является API-first продуктом.

UI должен оставаться максимально функциональным.

Главный принцип:

Configuration over decoration.

Precision over presentation.

Utility over dashboard aesthetics.

Не создавать элементы только потому, что они красиво выглядят в других продуктах Bellenne.

---

# 8. User Interface Scope

Предпочтительные разделы Nest:

Overview

Configuration

Presets

Materials

API

Jobs

Diagnostics

Settings

Фактическая структура должна определяться реальными функциями приложения.

Не создавать раздел, если соответствующей функциональности нет.

---

# 9. Overview

Nest Overview должен быть компактным.

Он может отображать:

Service Status

API Status

Current Configuration

Active Preset

Algorithm Version

Recent Jobs

Last Calculation

Errors / Warnings

Не создавать на Overview большое количество KPI без практической необходимости.

---

# 10. Configuration

Configuration является одной из основных страниц Nest.

Настройки должны быть организованы логическими секциями.

Примеры:

Layout

Material

Spacing

Constraints

Optimization

Output

Advanced

Названия должны соответствовать фактическим параметрам алгоритма.

Не создавать настройки, которых нет в backend.

---

# 11. Settings Structure

Для сложной конфигурации предпочтительна структура:

Section

→ Setting Group

→ Setting

Setting должен содержать:

Label

Control

Optional description

Optional validation state

Optional unit

Не использовать огромные карточки для каждой отдельной настройки.

---

# 12. Settings Density

Nest является техническим продуктом.

Допускается более высокая информационная плотность, чем в обычном consumer UI.

Несколько связанных настроек должны находиться в одном визуальном блоке.

Плохо:

Card
→ Width

Card
→ Height

Card
→ Gap

Card
→ Rotation

Хорошо:

Layout Settings

Width
Height
Gap
Rotation

в одной общей секции.

---

# 13. Forms

Использовать общие Bellenne form components.

Inputs

Selects

Checkboxes

Radio

Toggle

Textarea

Number Input

Все должны соответствовать `docs/BRANDBOOK.md`.

Не создавать отдельный "engineering UI kit".

---

# 14. Numeric Inputs

Nest содержит большое количество числовых настроек.

Numeric inputs должны:

* явно показывать единицу измерения;
* иметь корректные ограничения;
* поддерживать validation;
* не позволять неоднозначно интерпретировать значение.

Пример:

Gap

[ 5 ] mm

а не:

Gap

[ 5 ]

если единица измерения имеет значение.

---

# 15. Units

Единицы измерения должны отображаться последовательно.

Например:

mm

cm

px

%

Использовать единицу, соответствующую фактической бизнес-логике.

Не выполнять автоматическую конвертацию между единицами без явного требования.

---

# 16. Validation

Ошибки конфигурации необходимо показывать максимально точно.

Плохо:

Invalid settings

Хорошо:

Minimum gap cannot be greater than maximum gap.

Validation state должен использовать:

Danger color

message

при необходимости icon.

Не кодировать ошибку только красной рамкой.

---

# 17. Presets

Preset — сохранённый набор параметров Nest.

Типичные действия:

Create

Save

Duplicate

Rename

Set Active

Reset

Delete

Import

Export

Добавлять только те действия, которые реально поддерживает приложение.

---

# 18. Preset List

Preset list должен быть компактным.

Показывать только полезную информацию.

Например:

Name

Status

Updated

Configuration summary

Actions

Активный preset может использовать Nest Accent.

Не создавать для каждого preset огромную карточку.

---

# 19. Active Configuration

Пользователь всегда должен понимать:

какая конфигурация сейчас используется.

Если существует Active Preset:

его статус должен быть явно виден.

Например:

Active

Production Default

Не полагаться исключительно на цвет.

---

# 20. Unsaved Changes

При изменении настроек необходимо визуально различать:

Saved Configuration

Unsaved Changes

При наличии несохранённых изменений пользователь должен это понимать.

Не сохранять критические изменения автоматически, если существующая бизнес-логика требует явного Save.

---

# 21. Destructive Changes

Для действий вроде:

Reset

Delete Preset

Clear Configuration

Revoke API Key

использовать Danger semantics.

Для потенциально разрушительных операций требуется confirmation, если действие нельзя легко отменить.

---

# 22. API

API является одной из главных частей Nest.

API page может содержать:

Service URL

API Version

Authentication

API Keys

Endpoints

Request Limits

Timeouts

Status

Examples

Diagnostics

Добавлять только реально существующие функции.

---

# 23. API Keys

API keys являются чувствительными данными.

По умолчанию ключ должен быть скрыт.

Разрешённые действия:

Reveal при необходимости

Copy

Regenerate / Rotate

Revoke

Не отображать полный API key постоянно после создания, если это не требуется архитектурой приложения.

---

# 24. API Key Visual Style

API key не должен выглядеть как обычный текстовый input.

Предпочтительно:

monospace representation

masked value

compact action buttons

Пример:

sk_nest_••••••••••••4A21

Copy

Revoke

Не использовать яркую стилизацию.

---

# 25. Technical Values

Для:

IDs

API keys

job IDs

hashes

technical identifiers

допускается monospace font из системного monospace stack.

Это исключение из общего правила использования Inter.

Пример:

```css
font-family:
    ui-monospace,
    SFMono-Regular,
    Menlo,
    Monaco,
    Consolas,
    monospace;
```

Monospace использовать только для технических значений.

Не использовать его для обычного UI.

---

# 26. API Documentation

Если приложение содержит встроенное описание API:

оно должно быть функциональным и компактным.

Для endpoint отображать:

Method

Path

Description

Input

Output

Errors

Не пытаться самостоятельно превращать интерфейс в полный аналог Swagger/OpenAPI UI, если это не требуется.

---

# 27. HTTP Methods

Для HTTP methods допускаются компактные badges:

GET

POST

PUT

PATCH

DELETE

Цвет метода является вспомогательным.

Не создавать новую яркую цветовую систему специально для methods.

Приоритет:

readability.

---

# 28. API Test

Если существует функция тестового запроса, UI может иметь:

Request Parameters

Run Calculation

Request Body

Response

Duration

Status

Error

Это технический инструмент.

Не превращать его в декоративный API playground без необходимости.

---

# 29. Jobs

Если API calculation является асинхронным, Nest может отображать Jobs.

Типичные поля:

Job ID

Created

Status

Duration

Preset

Input Summary

Result

Error

Добавлять Jobs только если backend действительно использует соответствующую модель выполнения.

---

# 30. Job Status

Предпочтительные статусы:

Queued

Running

Completed

Failed

Cancelled

Использовать semantic colors.

Queued:

Neutral

Running:

Nest Accent или information state

Completed:

Success

Failed:

Danger

Cancelled:

Neutral

---

# 31. Calculation Result

Результат расчёта должен быть представлен в первую очередь как структурированные данные.

Например:

Efficiency

Used Area

Waste

Elements

Placement

Warnings

Runtime

Конкретный набор определяется API.

Не придумывать дополнительные показатели.

---

# 32. Layout Preview

Если приложение действительно отображает визуальный результат раскладки, использовать специальный Layout Preview component.

Preview является функциональной визуализацией.

Это не декоративная иллюстрация.

Он может отображать:

Material boundary

Placed elements

Unused area

Element IDs

Dimensions

Orientation

Warnings

Добавлять только те элементы, которые реально содержатся в результате расчёта.

---

# 33. Layout Preview Style

Layout Preview должен использовать Bellenne visual language.

Canvas:

dark neutral surface.

Material boundary:

muted border.

Placed elements:

Nest Accent с различимой границей.

Selected element:

stronger Nest Accent state.

Invalid element:

Danger.

Warning:

Warning color.

Не использовать случайные яркие цвета для каждого элемента без необходимости.

---

# 34. Element Differentiation

Если визуализация содержит множество элементов, не обязательно назначать каждому уникальный цвет.

Предпочтительные способы различения:

ID

outline

selection

pattern при необходимости

label

position

Цвет должен оставаться контролируемым.

---

# 35. Optimization Metrics

Если backend возвращает метрики эффективности, можно отображать:

Material Usage

Waste

Efficiency

Runtime

Placed Elements

Unplaced Elements

Но только если эти значения реально существуют в API.

Не генерировать фиктивные KPI.

---

# 36. Efficiency Semantics

Высокое использование материала может быть Positive.

Низкий Waste может быть Positive.

Но semantic interpretation должна соответствовать реальной бизнес-логике.

Не определять semantic state только по тому, увеличилось или уменьшилось число.

---

# 37. Algorithm Settings

Настройки алгоритма могут быть сложными.

Разделять:

Common Settings

и

Advanced Settings.

Advanced configuration по умолчанию может быть collapsed.

Не прятать часто используемые настройки в Advanced.

---

# 38. Advanced Settings

Advanced section предназначен для:

редко изменяемых;

низкоуровневых;

алгоритмических;

экспериментальных

параметров.

Не использовать Advanced как свалку для настроек, которые некуда положить.

---

# 39. Dangerous Algorithm Settings

Если параметр способен значительно ухудшить результат или производительность:

добавить concise warning.

Например:

Changing this value may significantly increase calculation time.

Не использовать modal confirmation для каждого технического параметра.

---

# 40. Default Values

Default values должны поступать из backend/configuration.

Codex не должен самостоятельно придумывать:

default width

default gap

default limits

default timeout

default optimization weights

или другие алгоритмические значения.

Если default отсутствует:

оставить его неопределённым или запросить решение.

---

# 41. Backend Is Source of Truth

Для всех algorithm-specific constraints backend является источником истины.

Frontend не должен самостоятельно дублировать сложную бизнес-логику, если она уже реализована на сервере.

Frontend validation может улучшать UX, но не заменяет server validation.

---

# 42. No Invented Parameters

Критическое правило.

Codex запрещено самостоятельно добавлять настройки алгоритма только потому, что они "обычно бывают в nesting software".

Например нельзя самостоятельно придумывать:

Rotation Optimization

Greedy Mode

Genetic Algorithm

Quality Level

Iterations

Threads

Compression

если подобных параметров нет в продукте.

UI должен отражать реальный API.

---

# 43. Diagnostics

Diagnostics page при необходимости может показывать:

Service Status

Algorithm Version

API Version

Environment

Last Error

Calculation Time

Queue

Dependency Status

Не отображать sensitive infrastructure information обычным пользователям без необходимости.

---

# 44. Service Status

Типичные состояния:

Operational

Degraded

Unavailable

Maintenance

Использовать semantic statuses.

Operational:

Success

Degraded:

Warning

Unavailable:

Danger

Maintenance:

Neutral / Information

---

# 45. Logs

Если Nest позволяет просматривать логи:

не использовать обычную DataTable для длинного raw log text, если специализированное компактное log view подходит лучше.

Log view:

dark surface

monospace

timestamp

level

message

Не добавлять terminal aesthetic сверх необходимого.

---

# 46. Overview Metrics

Overview не должен искусственно копировать BellennePulse.

Допустимы компактные status cards:

API Status

Active Preset

Last Job

Average Calculation Time

Current Version

Но только если данные действительно полезны.

Не создавать финансовые-style KPI cards для технических параметров без причины.

---

# 47. Navigation

После объединения продуктов ожидаемая global navigation:

Bellenne

Overview

Pulse

Echo

Vector

Nest

Settings

Nest должен находиться внутри общего Bellenne Shell.

---

# 48. Internal Navigation

Внутренняя структура Nest может быть:

Overview

Configuration

Presets

API

Jobs

Diagnostics

Settings

Но не создавать разделы заранее без функциональной необходимости.

---

# 49. Product Transition

Переход:

Pulse → Nest

Echo → Nest

Vector → Nest

не должен ощущаться как переход на другое приложение.

Не меняются:

global sidebar

global typography

buttons

inputs

modals

dropdowns

cards

spacing

radii

shadows

Меняются:

business context

internal navigation

Product Accent

functional content

---

# 50. Shared Components

Использовать общие Bellenne components.

Например:

Button

Card

Input

NumberInput

Select

Toggle

Tabs

Modal

Badge

Tooltip

DataTable

StatusBadge

EmptyState

FormSection

Не создавать:

NestButton

NestInput

NestModal

NestCard

если отличие заключается только в цвете или контексте.

---

# 51. Nest-specific Components

Product-specific компоненты должны описывать бизнес-функцию.

Хорошо:

LayoutPreview

PresetSelector

NestJobTable

APIKeyField

ConfigurationSection

CalculationResult

ServiceStatus

MaterialSettings

Плохо:

NestPurpleButton

NestCard

NestSelect

---

# 52. Empty States

Empty state должен быть коротким и полезным.

Пример:

No presets yet

Create a preset to save reusable optimization settings.

Не использовать огромные иллюстрации.

---

# 53. Loading

При расчёте использовать спокойный functional state.

Например:

Calculating layout...

Не использовать постоянно пульсирующие или сложные decorative animations.

Если backend предоставляет прогресс:

показывать реальное значение.

Не имитировать fake progress.

---

# 54. Calculation Duration

Если расчёт может занимать значительное время:

показывать:

Running

Elapsed Time

Progress при наличии

Cancel при наличии поддержки

Не блокировать весь интерфейс без необходимости.

---

# 55. Errors

Ошибки должны сообщать:

что произошло;

где произошло;

что можно сделать.

Пример:

Calculation failed

The supplied material width is smaller than one of the required elements.

Review configuration

Не показывать raw stack traces обычному пользователю.

---

# 56. Technical Error Details

Для административного режима может быть доступен:

Technical Details

с:

error code

request ID

job ID

raw message

Он должен быть secondary/collapsible.

---

# 57. API-first Architecture

Frontend должен рассматриваться как client Nest API.

Не внедрять algorithm/business logic в UI ради удобства, если она должна находиться в Nest Engine.

UI:

configures;

sends requests;

displays state;

displays result.

Engine:

calculates.

---

# 58. Future Integration

Nest может использоваться другими продуктами Bellenne через API.

Поэтому Product UI не должен предполагать, что все calculation requests обязательно создаются вручную через Nest interface.

Jobs могут приходить:

из API;

из других Bellenne modules;

из внешних систем.

Если backend предоставляет source information, его можно отображать.

---

# 59. Bellenne Ecosystem Role

Nest является service-oriented модулем.

В будущем Bellenne может содержать:

Pulse — Observe

Echo — Respond

Vector — Promote

Nest — Optimize

Nest не обязан иметь такой же объём UI, как остальные продукты.

Visual consistency не означает functional symmetry.

---

# 60. No Dashboard Inflation

Критическое правило.

Не превращать Nest в dashboard-heavy приложение только ради соответствия другим продуктам.

Если интерфейс состоит из:

Settings

API

Presets

Service Status

этого достаточно.

Не придумывать:

charts;

analytics;

KPIs;

history;

reports

без бизнес-потребности.

---

# 61. No Visual Drift

BellenneNest не имеет отдельного UI kit.

Запрещено создавать:

новую typography;

новую card system;

новую form system;

новую sidebar;

новую modal system;

новые radii;

собственные shadows;

отдельную engineering theme;

terminal-inspired theme;

CAD-inspired theme.

Nest является частью Bellenne.

---

# 62. Definition of Done

Перед завершением Nest UI-задачи проверить:

* прочитан `docs/BRANDBOOK.md`;
* прочитан `docs/products/NEST.md`;
* использованы общие Bellenne components;
* Nest Accent используется только как Product Accent;
* semantic colors используются корректно;
* не появились arbitrary colors;
* не появились arbitrary spacing values;
* не создан отдельный visual language;
* UI отражает реальный API;
* не придуманы несуществующие algorithm settings;
* единицы измерения понятны;
* numeric fields имеют validation;
* technical identifiers оформлены последовательно;
* sensitive API data защищены;
* не создан лишний dashboard;
* не добавлены фиктивные KPI;
* интерфейс совместим с будущим Bellenne Shell.

Исправить нарушения до завершения задачи.

---

# 63. Final Principle

BellenneNest is the optimization engine of the Bellenne ecosystem.

It should feel:

Precise.

Technical.

Controlled.

Efficient.

But still unmistakably Bellenne.

Same ecosystem.

Same design language.

Different purpose.

Configuration over decoration.

Precision over presentation.
