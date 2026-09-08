# BellenneProof Product Design Specification

## 1. Product

Official name:

BellenneProof

Short module name:

Proof

Product category:

Automated Color Proof Orchestration & Production Control

BellenneProof — модуль экосистемы Bellenne для управления процессом автоматической подготовки цветопроб.

Система получает задания из внешних систем, в том числе CRM, создаёт Job, помещает его в очередь, назначает доступному Worker, контролирует выполнение и сохраняет результат.

Непосредственная обработка файлов выполняется отдельным приложением:

Proof Worker.

Серверная управляющая часть:

Proof Core.

Основные задачи BellenneProof:

* приём webhook-запросов;
* создание заданий;
* управление очередью;
* управление Workers;
* контроль выполнения заданий;
* управление presets;
* просмотр результатов;
* просмотр ошибок;
* повторный запуск заданий;
* контроль интеграций;
* управление настройками;
* просмотр журналов событий;
* диагностика Proof Core и Proof Workers.

---

# 2. Design System

BellenneProof НЕ имеет собственной независимой дизайн-системы.

Обязательный источник:

`docs/BRANDBOOK.md`

Все интерфейсы Proof должны использовать общую Bellenne Design System.

Proof должен визуально восприниматься как часть той же платформы, что:

BellennePulse

BellenneEcho

BellenneVector

BellenneNest

---

# 3. Product Role

BellenneProof является operational / orchestration продуктом.

Он не является:

* BI dashboard;
* графическим редактором;
* Photoshop replacement;
* файловым менеджером;
* CRM;
* полноценной MES;
* отдельным desktop UI для Worker.

Основная роль интерфейса:

Observe.

Control.

Diagnose.

Retry.

Configure.

Proof UI управляет процессом.

Proof Worker выполняет обработку.

---

# 4. Product Architecture

Основные архитектурные сущности:

Proof Core

Proof Worker

Proof Node

Proof Job

Proof Queue

Proof Preset

Proof Result

Proof Integration

Proof Event

---

# 5. Terminology

## Proof Core

Центральная серверная часть системы.

Отвечает за:

* API;
* webhook endpoints;
* очередь;
* управление заданиями;
* управление Workers;
* настройки;
* результаты;
* интеграции;
* логи.

---

## Proof Worker

Отдельное приложение, выполняющее реальную обработку файлов.

Worker может:

* получать Job;
* искать исходный файл;
* выполнять анализ;
* выбирать область цветопробы;
* формировать результат;
* отправлять результат обратно в Proof Core.

Фактические возможности должны определяться реальной реализацией Worker.

UI не должен придумывать функции Worker.

---

## Proof Node

Компьютер или сервер, на котором работает Proof Worker.

Один Node может соответствовать одному Worker либо другой архитектуре, если она будет реализована.

Не путать Node и Worker, если backend различает эти понятия.

---

## Proof Job

Одна задача подготовки цветопробы.

Job является основной операционной сущностью интерфейса.

---

## Proof Queue

Очередь заданий, ожидающих выполнения.

---

## Proof Result

Результат обработки Job.

---

## Proof Preset

Сохранённая конфигурация обработки.

---

# 6. Product Identity

Основные идеи Proof:

Production.

Automation.

Control.

Flow.

Reliability.

Precision.

Traceability.

Цветопроба является предметной областью продукта.

Но визуальный стиль не должен буквально имитировать полиграфию.

Не использовать:

* CMYK blobs повсюду;
* цветовые круги;
* краску;
* принтеры;
* декоративные изображения рулонов;
* фотошопоподобный интерфейс.

Proof должен выглядеть как современный control plane.

---

# 7. Product Accent

Proof Product Accent:

#E94D8A

Название:

Proof Rose

Token:

```css
--product-accent: #E94D8A;
```

Proof Rose используется для:

* Proof product icon;
* active Proof navigation;
* selected Job;
* selected Worker;
* active Queue item;
* focus state;
* product-specific primary action;
* небольших highlights;
* selected preset;
* связи Job → Worker → Result;
* отдельных элементов визуализации процесса.

Proof Rose НЕ является:

* Error;
* Warning;
* Success.

---

# 8. Semantic Colors

Общие semantic colors Bellenne сохраняются.

Success:

#22D3A5

Danger:

#FF5C70

Warning:

#F59E0B

Neutral:

#AAB3C2

Proof Accent:

#E94D8A

Пример:

Completed Job → Success

Failed Job → Danger

Queued Job → Neutral

Running Job → Proof Accent / informational running state

Retry scheduled → Warning или Neutral в зависимости от логики

Не использовать Proof Rose для Failed.

---

# 9. Main UX Principle

Proof является системой контроля процессов.

Главный UX-принцип:

State must always be obvious.

Пользователь должен быстро понимать:

Что сейчас происходит?

Что стоит в очереди?

Какое задание выполняется?

На каком Worker оно выполняется?

Есть ли ошибки?

Где находится результат?

Почему Job завершился ошибкой?

Можно ли его повторить?

---

# 10. Recommended Navigation

Предпочтительная структура:

Overview

Jobs

Queue

Workers

Results

Presets

Integrations

Logs

Settings

Добавлять раздел только если соответствующая функциональность реально существует.

---

# 11. Overview

Overview должен быть операционным.

Допустимые данные:

Core Status

Workers Online

Queue Size

Running Jobs

Failed Jobs

Completed Today

Recent Errors

Recent Jobs

Последние события

Не превращать Overview в финансовый BI dashboard.

---

# 12. Overview Priority

На Overview приоритет информации:

1. Есть ли проблемы.
2. Работают ли Workers.
3. Есть ли очередь.
4. Выполняются ли Jobs.
5. Есть ли Failed Jobs.
6. Что произошло недавно.

Статус системы важнее красивых KPI.

---

# 13. Status Summary

Допускаются компактные status cards.

Пример:

Core

Operational

Workers

3 / 3 online

Queue

12 jobs

Running

2 jobs

Failed

1 job

Использовать общий Bellenne MetricCard / StatusCard pattern.

---

# 14. Job Lifecycle

Job должен иметь явно определённый lifecycle.

Использовать только реальные backend statuses.

Пример возможной модели:

Received

Queued

Assigned

Running

Uploading

Completed

Failed

Cancelled

Retrying

Это пример терминологии.

Не создавать статусы, если backend их не поддерживает.

---

# 15. Job Status Semantics

Типовое визуальное отображение:

Received:
Neutral

Queued:
Neutral

Assigned:
Information / Proof Accent

Running:
Proof Accent

Uploading:
Proof Accent

Completed:
Success

Failed:
Danger

Cancelled:
Neutral

Retrying:
Warning

Точная модель должна соответствовать backend.

---

# 16. Job List

Jobs является одной из главных страниц Proof.

Предпочтительно использовать плотную DataTable.

Пример колонок:

Job ID

Created

Source

Order / CRM Entity

Preset

Status

Worker

Duration

Result

Actions

Фактические колонки должны определяться данными приложения.

---

# 17. Job ID

Job ID является техническим идентификатором.

Допускается monospace.

Пример:

```text
PF-20260907-01842
```

Не генерировать собственный формат ID во frontend.

Frontend отображает значение backend.

---

# 18. Job Selection

Selected Job может использовать:

Proof Accent border / background с небольшой opacity.

Не окрашивать всю строку ярко.

---

# 19. Job Details

Job Details должен давать полный контекст выполнения.

Предпочтительные секции:

Summary

Input

Processing

Worker

Timeline

Result

Errors

Technical Details

Показывать только реально существующие данные.

---

# 20. Job Summary

Summary может содержать:

Job ID

Status

Created

Started

Finished

Duration

Source

Preset

Worker

Не дублировать одинаковые значения в нескольких местах страницы.

---

# 21. Job Timeline

Timeline является хорошим паттерном для Proof.

Он может показывать последовательность событий:

Webhook received

Job created

Queued

Worker assigned

Processing started

Result created

Uploaded

Completed

Если Job failed:

Failure event

Тimeline должен основываться на реальных событиях.

Не генерировать фиктивные этапы.

---

# 22. Timeline Visual Style

Timeline должен быть компактным.

Completed step:

Success / neutral completed state

Current step:

Proof Accent

Failed step:

Danger

Future / unavailable:

Muted

Не использовать огромную вертикальную stepper-композицию без необходимости.

---

# 23. Queue

Queue page предназначена для операционного контроля.

Основные задачи:

* увидеть очередь;
* определить порядок;
* увидеть ожидающие Jobs;
* увидеть назначенные Jobs;
* понять потенциальную проблему.

---

# 24. Queue List

Предпочтительные данные:

Position

Job

Created

Priority

Preset

Status

Assigned Worker

Waiting Time

Добавлять Priority только если такая функция реально существует.

---

# 25. Queue Ordering

Не реализовывать drag-and-drop reorder, если backend не поддерживает изменение порядка.

UI не должен обещать возможности, которых нет.

---

# 26. Priority

Если Job имеет priority:

Low

Normal

High

Critical

или другую модель —

использовать реальную модель backend.

Не вводить priority самостоятельно.

Critical priority не равно Failed.

Semantic meaning должен быть понятен.

---

# 27. Workers

Workers является ключевой операционной страницей.

Пользователь должен быстро понимать:

* какие Workers зарегистрированы;
* какие online;
* какие заняты;
* какой Job выполняют;
* когда был последний heartbeat;
* есть ли ошибка;
* какая версия Worker используется.

Показывать только реально доступную информацию.

---

# 28. Worker Status

Возможная модель:

Online

Busy

Offline

Error

Disabled

Updating

Использовать только backend-supported states.

---

# 29. Worker Status Semantics

Online:

Success

Busy:

Proof Accent

Offline:

Neutral или Danger в зависимости от контекста

Error:

Danger

Disabled:

Neutral

Updating:

Information

---

# 30. Worker List

Предпочтительно использовать DataTable или compact cards при небольшом количестве Workers.

Возможные поля:

Worker

Node

Status

Current Job

Last Seen

Version

Jobs Completed

Actions

Не создавать analytics metrics, если они не нужны пользователю.

---

# 31. Worker Heartbeat

Если backend использует heartbeat:

показывать:

Last seen 12 sec ago

а не только:

Online

При потере heartbeat статус должен определяться backend или общей системой.

Frontend не должен самостоятельно придумывать сложные правила offline detection, если они реализованы на сервере.

---

# 32. Worker Details

Worker Details может содержать:

Identity

Status

Current Job

Version

Capabilities

Last Heartbeat

Recent Jobs

Recent Errors

Configuration

Capabilities показывать только если Worker действительно их сообщает.

---

# 33. Worker Version

Версию отображать технически и компактно.

Пример:

v1.4.2

Если Core и Worker имеют несовместимые версии, можно показывать warning только при наличии соответствующей логики backend.

---

# 34. Results

Results — результаты завершённых Jobs.

Result может содержать:

Preview

Output File

Created

Job

Preset

Worker

Metadata

CRM delivery status

Добавлять только реально доступные поля.

---

# 35. Proof Preview

Если результатом является изображение цветопробы, допускается визуальный Preview.

Preview является функциональным.

Он может показывать:

готовый proof image;

увеличенный просмотр;

основную информацию о результате.

Не превращать Result page в графический редактор.

---

# 36. Preview Editing

BellenneProof Control UI НЕ должен автоматически получать инструменты:

crop;

brush;

manual selection;

color correction;

image editing

если такая функциональность явно не заложена в продукт.

Preview — просмотр результата.

Не editor.

---

# 37. Result File

Для готового файла могут быть доступны действия:

Open

Download

Copy Link

Send Again

Retry Upload

Delete

Только если backend поддерживает соответствующее действие.

---

# 38. CRM Delivery

Если Proof отправляет результат обратно в CRM, статус доставки должен быть отдельным состоянием от Job Status.

Пример:

Job:
Completed

CRM Delivery:
Delivered

или:

CRM Delivery:
Failed

Не превращать ошибку callback/upload в Failed processing, если сама цветопроба была успешно создана.

---

# 39. Processing vs Delivery

Критическое правило.

Различать:

Processing Status

и

Delivery Status.

Например:

Processing:
Completed

Delivery:
Failed

Это разные проблемы и требуют разных действий.

---

# 40. Integrations

Integrations page отвечает за внешние системы.

Например:

CRM

webhooks

callback endpoints

storage

other services

Добавлять только реально существующие integrations.

---

# 41. Integration Card

Integration должна показывать:

Name

Status

Last Activity

Configuration Summary

Errors

Actions

Не использовать отдельный визуальный стиль для каждой CRM.

---

# 42. Webhooks

Webhook configuration может содержать:

Endpoint

Status

Last Request

Authentication

Events

Recent Errors

Показывать только реально существующую конфигурацию.

---

# 43. Incoming Webhook

Для входящего события может быть полезен технический просмотр:

Received At

Source

Event Type

Request ID

Linked Job

Status

Technical payload при необходимости

Payload должен находиться в Technical Details, а не доминировать в основном UI.

---

# 44. Payload Display

Raw JSON отображать:

monospace

dark elevated surface

syntax highlighting при наличии существующего компонента

copy action

Не строить отдельный JSON-editor, если пользователь только просматривает payload.

---

# 45. Presets

Preset определяет настройки подготовки цветопробы.

Preset UI должен использовать общую Bellenne form system.

Возможные действия:

Create

Edit

Duplicate

Activate

Archive

Delete

Использовать только реально поддерживаемые действия.

---

# 46. Preset Settings

Codex НЕ должен самостоятельно придумывать параметры цветопробы.

UI должен отражать реальную конфигурационную модель.

Не добавлять самостоятельно:

crop size

DPI

margins

thumbnail size

quality

selection mode

color profile

или другие параметры

только потому, что они кажутся логичными.

Если параметр отсутствует в backend/specification — его нет в UI.

---

# 47. Active Preset

Если существует понятие default / active preset:

оно должно быть явно видно.

Использовать:

text label

и небольшой Proof Accent.

Не обозначать состояние только цветом.

---

# 48. Logs

Logs являются диагностическим инструментом.

Поддерживаемые типы могут включать:

Core

Worker

Job

Integration

Webhook

System

Использовать только реальные log sources.

---

# 49. Log View

Log View:

* compact;
* monospace message;
* timestamp;
* level;
* source;
* optional Job ID;
* optional Worker ID.

Не создавать стилизацию терминала ради эстетики.

---

# 50. Log Levels

Если backend использует:

Debug

Info

Warning

Error

Critical

отображать их с общей semantic system.

Не использовать Proof Accent как log severity.

---

# 51. Error Handling

Ошибка должна сообщать:

What happened

Where

Affected Job / Worker

Possible action

Technical information при необходимости

Пример:

Job failed

Worker could not locate the source file.

Worker:
PC-PROOF-02

Job:
PF-1842

Retry

View details

---

# 52. Technical Details

Technical information должна быть secondary.

Для обычного состояния показывать человеку понятное сообщение.

Дополнительный expandable block:

Technical Details

может содержать:

error code

stack trace

request ID

job ID

worker logs

raw response

---

# 53. Retry

Retry является важным действием Proof.

Если backend поддерживает retry:

пользователь должен понимать, что будет повторено.

Например:

Retry Job

Retry Delivery

Retry File Lookup

Это могут быть разные действия.

Не использовать один абстрактный Retry, если последствия неоднозначны.

---

# 54. Dangerous Actions

Для:

Cancel Job

Delete Result

Disable Worker

Clear Queue

Delete Preset

Revoke integration credentials

использовать Danger semantics и confirmation при необходимости.

Не использовать confirmation для безопасных действий вроде Copy ID.

---

# 55. Manual Job Creation

Если система позволяет создать Job вручную:

использовать тот же pipeline, что и webhook Jobs.

Manual Job не должен иметь отдельную визуальную модель.

Source:

Manual

может отличать его от CRM Job.

Не добавлять manual creation, если backend этого не поддерживает.

---

# 56. Source

Job Source должен быть отдельным полем.

Примеры:

CRM

API

Manual

Retry

System

Точные значения определяются backend.

---

# 57. Real-time Updates

Proof является operational system.

Если архитектура поддерживает realtime events, интерфейс должен обновлять:

Job statuses

Queue

Worker state

Errors

без полной перезагрузки страницы.

Не имитировать realtime через визуальные эффекты.

---

# 58. Auto Refresh

Если realtime отсутствует и используется polling:

пользователь не обязан видеть техническую механику polling.

При необходимости можно отображать:

Last updated

Refresh

Не делать постоянно вращающиеся refresh icons.

---

# 59. Notifications

Важные события:

Worker offline

Job failed

Integration failed

Queue blocked

Core degraded

могут использовать alerts.

Не показывать toast на каждое успешное завершение Job при высокой нагрузке.

Avoid notification spam.

---

# 60. Queue Health

Если backend предоставляет информацию о здоровье очереди, она может отображаться.

Например:

Queue operational

Queue delayed

Queue unavailable

Не придумывать queue health algorithms во frontend.

---

# 61. Operational Density

Proof рассчитан на быстрый мониторинг процессов.

Интерфейс должен быть достаточно плотным.

Предпочитать:

tables

status lists

compact detail panels

filters

timeline

Плохо:

огромные SaaS cards

большие декоративные headers

много пустого пространства

---

# 62. Filters

Типичные фильтры Jobs могут включать:

Status

Period

Worker

Preset

Source

Result State

Integration State

Добавлять только фильтры, которые поддерживаются данными.

---

# 63. Search

Если реализован поиск, он может работать по:

Job ID

Order ID

CRM entity

Worker

filename

или другим реальным индексируемым данным.

Не обещать поиск по данным, которые backend не умеет искать.

---

# 64. Empty States

Примеры:

No jobs in queue

Queue is currently empty.

No workers connected

Connect a Proof Worker to start processing jobs.

No failed jobs

Everything is running normally.

Empty state должен быть кратким.

Не использовать огромные иллюстрации.

---

# 65. Loading States

Использовать общую Bellenne loading system.

Для таблиц:

skeleton rows.

Для Job Details:

section skeleton.

Для action:

button loading state.

Не блокировать весь интерфейс одним fullscreen loader без необходимости.

---

# 66. Worker Installation

Если Core UI предоставляет информацию для установки Worker, допустим отдельный onboarding section.

Например:

Download Worker

Node token

Connection status

Installation guide

Только если такая функциональность действительно существует.

---

# 67. Secrets

API keys

Worker tokens

webhook secrets

CRM credentials

и другие sensitive values

по умолчанию должны быть masked.

Не отображать secret в:

logs

tables

error messages

activity timeline

---

# 68. Monospace

Для технических значений допускается:

```css
font-family:
    ui-monospace,
    SFMono-Regular,
    Menlo,
    Monaco,
    Consolas,
    monospace;
```

Использовать для:

Job IDs

Worker IDs

Request IDs

API endpoints

tokens

file paths

technical payloads

Не использовать monospace для обычного UI.

---

# 69. File Paths

Proof может работать с Windows / network file paths.

Пути следует показывать:

monospace

copyable

с ellipsis в таблице

полностью в tooltip/details.

Пример:

`\\storage\designs\article\file.tif`

Не переносить длинный путь так, чтобы он ломал таблицу.

---

# 70. File Names

Filename является важным техническим контекстом.

Отображать оригинальное имя файла без визуального изменения.

Не пытаться humanize filename.

---

# 71. Worker Logs vs Core Logs

Если доступны оба типа:

визуально показывать Source.

Например:

CORE

WORKER-02

INTEGRATION

Но использовать общий Log View.

Не создавать отдельный дизайн логов для Worker.

---

# 72. Core Status

Proof Core может иметь системный status:

Operational

Degraded

Unavailable

Maintenance

Использовать общие semantic colors Bellenne.

---

# 73. System Health

System Health может объединять:

Core

Queue

Workers

Integrations

Но только если данные реально доступны.

Не создавать fake overall health score.

---

# 74. No Fake Metrics

Критическое правило.

Не придумывать:

Average Quality

Optimization Score

AI Confidence

Worker Efficiency

Color Accuracy

Success Score

или другие KPI без реальной бизнес-метрики.

UI показывает только фактические данные системы.

---

# 75. No Fake AI

Если часть обработки использует computer vision / AI:

это техническая реализация.

Не создавать отдельный AI visual style.

Не использовать:

sparkles

magic gradients

AI assistant avatars

glowing AI cards

без функциональной необходимости.

---

# 76. Preview Color Handling

Preview готовой цветопробы является контентом пользователя.

Product Accent не должен накладываться на preview.

Не:

tint preview;

recolor preview;

apply gradients;

modify proof image.

Preview должен показывать реальный результат максимально нейтрально.

---

# 77. Image Background

Для просмотра proof image использовать нейтральный background.

Предпочтительно:

dark surface

или checkerboard только если transparency имеет значение.

Не использовать яркий Product Accent за изображением.

---

# 78. Worker UI Separation

Proof Worker является отдельным техническим приложением.

Не предполагать, что Worker должен иметь полный BellenneProof Control UI.

Worker interface, если он существует, должен быть минимальным.

Например:

Connection

Status

Current Job

Logs

Settings

Version

Основное управление выполняется через Proof Core.

---

# 79. Future Worker Scaling

Архитектура UI не должна предполагать существование только одного Worker.

Даже если сейчас Worker один, интерфейс должен корректно воспринимать Workers как collection, если backend архитектура допускает масштабирование.

Не усложнять backend ради гипотетического масштабирования.

---

# 80. Bellenne Shell

После объединения продуктов ожидаемая global navigation:

Bellenne

Overview

Pulse

Echo

Vector

Nest

Proof

Settings

Proof работает внутри общего Bellenne Shell.

---

# 81. Product Transition

Переход:

Pulse → Proof

Vector → Proof

Nest → Proof

не должен выглядеть как переход на другой сайт.

Не меняются:

global navigation

typography

buttons

forms

tables

cards

modals

dropdowns

spacing

radii

shadows

Меняются:

business context

internal navigation

Product Accent

operational content

---

# 82. Shared Components

Использовать общие Bellenne components:

Button

Card

StatusCard

DataTable

Input

Select

Badge

Tooltip

Modal

Tabs

FilterBar

EmptyState

Alert

Drawer

Timeline

CodeBlock

LogView

если они существуют.

Не создавать:

ProofButton

ProofModal

ProofTable

ProofInput

только из-за принадлежности к модулю Proof.

---

# 83. Proof-specific Components

Допустимы бизнесовые компоненты:

JobStatus

JobTimeline

JobTable

QueueTable

WorkerStatus

WorkerTable

ProofPreview

ResultPanel

PresetSelector

WebhookEvent

IntegrationStatus

LogEntry

RetryAction

Это product/business components.

---

# 84. Relationship with Nest

Nest и Proof являются разными системами.

Nest:

optimization engine.

Proof:

production workflow orchestration.

Оба могут иметь Workers/API/backend concepts, но не объединять бизнесовые компоненты только из-за похожей технической архитектуры.

Общие primitive components должны переиспользоваться.

Business components остаются product-specific.

---

# 85. Relationship with Pulse

Pulse является визуальным baseline Bellenne.

Proof должен использовать тот же визуальный язык.

Но Proof не должен копировать структуру аналитического dashboard Pulse там, где нужен operational workflow UI.

Visual consistency не означает одинаковую information architecture.

---

# 86. Main Operational Screen

Если нужен главный рабочий экран Proof, предпочтительный приоритет:

System Status

Queue

Running Jobs

Failed Jobs

Workers

Recent Activity

Не:

10 больших KPI

4 декоративных графика

маркетинговый hero block

---

# 87. Actions Hierarchy

На странице Job:

Primary:

одно главное действие, если оно есть.

Например:

Retry Job

Secondary:

View Result

Open Source

Copy ID

Cancel

Technical Details

Не делать все действия Product Accent.

---

# 88. Auditability

Для производственного процесса полезна трассируемость.

Если backend хранит данные, UI должен позволять понять:

когда Job создан;

кто/что его создало;

какой Worker выполнил;

какой preset использовался;

когда результат создан;

куда результат отправлен;

какие ошибки происходили.

Не придумывать audit данные, которых backend не хранит.

---

# 89. Configuration Source of Truth

Backend является источником истины для:

Job states

Worker states

Preset fields

Queue behavior

Retry behavior

Integration settings

processing parameters

Frontend не должен самостоятельно моделировать бизнес-логику, которой нет на сервере.

---

# 90. No Invented Workflow

Codex запрещено самостоятельно придумывать этапы обработки.

Если реальный workflow:

Webhook

→ Queued

→ Worker

→ Complete

то UI не должен внезапно добавлять:

Validation

AI Analysis

Preflight

Rendering

Quality Check

Approval

Upload

если этих этапов фактически нет.

---

# 91. Design Priority

При реализации Proof:

1. Existing Bellenne shared component.
2. Existing Pulse visual pattern.
3. `docs/BRANDBOOK.md`.
4. This PROOF specification.
5. Minimal new pattern required by Proof business logic.

Не создавать новый visual language.

---

# 92. Definition of Done

Перед завершением любой Proof UI-задачи проверить:

* прочитан `docs/BRANDBOOK.md`;
* прочитан `docs/products/PROOF.md`;
* используются shared Bellenne components;
* Proof Accent используется только как Product Accent;
* semantic status colors имеют правильный смысл;
* Job lifecycle соответствует backend;
* Worker states соответствуют backend;
* не придуманы новые processing stages;
* не придуманы новые preset parameters;
* Processing Status отделён от Delivery Status;
* sensitive credentials скрыты;
* preview не изменяет изображение;
* интерфейс остаётся data-dense;
* нет fake analytics;
* нет fake AI UI;
* нет незапрошенного redesign;
* Proof совместим с общим Bellenne Shell.

---

# 93. Final Principle

BellenneProof is the production orchestration layer of the Bellenne ecosystem.

It should feel:

Controlled.

Reliable.

Traceable.

Fast.

Operational.

The user should always know:

what is waiting;

what is running;

where it is running;

what failed;

why it failed;

and what result was produced.

Same Bellenne design language.

Different operational purpose.

Control over decoration.

State over spectacle.

Reliability over novelty.
