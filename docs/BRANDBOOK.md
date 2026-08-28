# BellenneOne Design System

## 1. Назначение

Этот документ является единым источником истины для визуальной системы всей экосистемы BellenneOne.

Он применяется ко всем продуктам:

* BellennePulse
* BellenneEcho
* BellenneVector
* будущим продуктам Bellenne

Все продукты должны выглядеть как части одной системы.

Нельзя создавать отдельный визуальный язык для каждого приложения.

Основное правило:

One ecosystem.
One design system.
Different products.

---

# 2. Архитектура бренда

BellenneOne — зонтичный бренд.

Продукты являются модулями одной экосистемы.

Текущая продуктовая структура:

BellennePulse
Business intelligence и общая аналитика бизнеса.

BellenneEcho
Отзывы, ответы клиентам, автоматизация коммуникации и управление репутацией.

BellenneVector
Аналитика рекламы, продвижения и эффективности маркетинга.

В будущем продукты могут быть объединены в одно приложение Bellenne.

Поэтому интерфейсы должны проектироваться так, чтобы их можно было объединить без redesign.

---

# 3. Главный принцип

BellennePulse является исходной визуальной точкой всей экосистемы.

Общая концепция:

Dark BI Pulse

Она распространяется на всю платформу Bellenne.

Основные характеристики:

* dark-first интерфейс;
* premium business software;
* высокая информационная плотность;
* минимализм;
* data-first;
* строгая визуальная иерархия;
* холодные тёмные поверхности;
* контролируемые яркие акценты;
* минимум декоративных элементов;
* единая компонентная система.

Интерфейс не должен выглядеть:

* игровым;
* cyberpunk;
* crypto;
* маркетинговым landing page;
* как набор разных UI-kit.

---

# 4. Product Accent System

Каждый продукт Bellenne имеет собственный идентификационный accent.

Accent используется только для:

* иконки продукта;
* active navigation indicator;
* небольших highlights;
* primary chart series, относящихся именно к этому продукту;
* product badge;
* отдельных focus accents.

Accent НЕ меняет общую дизайн-систему.

## BellennePulse

Product Accent:

#7B3CFF

Название:

Pulse Violet

---

## BellenneEcho

Product Accent:

#06B6D4

Название:

Echo Cyan

---

## BellenneVector

Product Accent:

#2563EB

Название:

Vector Blue

---

# 5. Главный брендовый градиент

Bellenne Brand Gradient:

#7B3CFF → #2563EB

CSS:

```css
linear-gradient(
    90deg,
    #7B3CFF 0%,
    #2563EB 100%
)
```

Он используется для самого бренда Bellenne.

Product Accent не заменяет Bellenne Brand Gradient.

---

# 6. Основная палитра

## Background

Canvas:

#0F1420

Primary Surface:

#1A2233

Elevated Surface:

#2A3346

---

## Typography

Primary Text:

#F8FAFC

Secondary Text:

#AAB3C2

Muted Text:

#6B7587

---

## Brand

Bellenne Violet:

#7B3CFF

Secondary Violet:

#A855F7

Bellenne Blue:

#2563EB

Bellenne Cyan:

#06B6D4

---

## Semantic

Positive:

#22D3A5

Negative:

#FF5C70

Warning:

#F59E0B

---

# 7. Критическое правило цветов

Product Accent является идентификатором продукта, а не универсальным статусным цветом.

Например:

в BellenneEcho cyan не означает success.

Success всегда:

#22D3A5

Error всегда:

#FF5C70

Warning всегда:

#F59E0B

Нельзя менять семантические цвета под цвет текущего продукта.

---

# 8. Canonical Design Tokens

Все приложения Bellenne должны использовать общие токены.

```css
:root {
    color-scheme: dark;

    --b-bg: #0F1420;
    --b-surface: #1A2233;
    --b-surface-elevated: #2A3346;

    --b-text: #F8FAFC;
    --b-text-secondary: #AAB3C2;
    --b-text-muted: #6B7587;

    --b-violet: #7B3CFF;
    --b-violet-secondary: #A855F7;
    --b-blue: #2563EB;
    --b-cyan: #06B6D4;

    --b-success: #22D3A5;
    --b-danger: #FF5C70;
    --b-warning: #F59E0B;

    --b-border: rgba(255, 255, 255, 0.08);
    --b-border-strong: rgba(255, 255, 255, 0.12);

    --b-brand-subtle: rgba(123, 60, 255, 0.08);
    --b-brand-hover: rgba(123, 60, 255, 0.12);
    --b-brand-selected: rgba(123, 60, 255, 0.18);

    --b-radius-sm: 6px;
    --b-radius-md: 10px;
    --b-radius-lg: 12px;
    --b-radius-xl: 16px;

    --b-gradient-brand:
        linear-gradient(
            90deg,
            #7B3CFF 0%,
            #2563EB 100%
        );

    --b-shadow-card:
        0 8px 30px rgba(0, 0, 0, 0.20);

    --b-shadow-elevated:
        0 12px 40px rgba(0, 0, 0, 0.28);
}
```

Дополнительно активное приложение определяет:

```css
--product-accent
```

Пример BellenneEcho:

```css
--product-accent: #06B6D4;
```

Нельзя создавать отдельные независимые темы для Pulse, Echo и Vector.

---

# 9. Типографика

Основной UI font:

Inter

Fallback:

Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif

Разрешённые веса:

400
500
600
700

Не использовать дополнительные шрифты.

## Scale

Page title:
28px / 34px / 600

Section title:
18px / 24px / 600

Card title:
14px / 20px / 600

Body:
14px / 20px / 400

Secondary:
13px / 18px / 400

Small:
12px / 16px / 500

KPI:
28px / 32px / 600

Large KPI:
32px / 38px / 600

Для цифр:

```css
font-variant-numeric: tabular-nums;
```

---

# 10. Spacing

Использовать систему:

4px
8px
12px
16px
20px
24px
32px
40px
48px

Основная сетка:

4 / 8px

Default page padding:

24px

Grid gap:

16–20px

Section gap:

24–32px

Не создавать случайные spacing values.

---

# 11. Radius

Badges:

6px

Inputs:

10px

Buttons:

10px

Cards:

12px

Large panels:

16px

Не использовать radius 24px+ без специальной причины.

---

# 12. Общий Application Shell

Все продукты должны быть совместимы с единым Bellenne Shell.

Будущая объединённая структура предполагается примерно такой:

Bellenne

Navigation:

Overview

Pulse

Echo

Vector

Settings

---

## Sidebar

Один sidebar для всей платформы.

Product navigation должна работать внутри него, а не создавать отдельную навигационную систему для каждого продукта.

Основной фон:

#0F1420

Border:

rgba(255,255,255,0.08)

Menu item height:

40–44px

Radius:

8px

Product active indicator использует Product Accent.

---

# 13. Переключение продуктов

При нахождении пользователя внутри конкретного продукта:

Pulse → Violet accent

Echo → Cyan accent

Vector → Blue accent

При этом:

* background не меняется;
* sidebar не меняется;
* typography не меняется;
* buttons не получают новую геометрию;
* card style не меняется;
* tables не меняются;
* forms не меняются.

Меняется только идентификационный accent.

---

# 14. Buttons

## Primary Platform Action

Основное глобальное действие Bellenne:

Brand Gradient

#7B3CFF → #2563EB

---

## Product Primary Action

Если действие явно относится к конкретному продукту, допускается использование Product Accent.

Например в BellenneEcho:

Send reply

может использовать Echo Cyan.

Но не использовать accent для всех кнопок.

В одном контексте должен быть один очевидный primary action.

---

# 15. Cards

Все продукты используют одну систему карточек.

Background:

#1A2233

Border:

1px solid rgba(255,255,255,0.08)

Radius:

12px

Padding:

16–20px

Карточки BellenneEcho не должны отличаться по форме от карточек BellennePulse.

---

# 16. Forms

Input:

Background:
#1A2233

Border:
rgba(255,255,255,0.08)

Height:
40px

Radius:
10px

Focus border:

Product Accent

Focus ring:

product accent с opacity примерно 18%.

---

# 17. Tables

Все продукты используют одну DataTable system.

Numbers:

right aligned

Text:

left aligned

Header:

12px / 600

Rows:

минимум 44px

Divider:

rgba(255,255,255,0.06)

Hover:

rgba(255,255,255,0.03)

Active/select state:

Product Accent с низкой opacity.

---

# 18. Charts

Графики должны использовать общую цветовую систему.

Product Accent используется как основная серия продукта.

Secondary series должны использовать существующую Bellenne palette.

Нельзя создавать новую палитру графиков для каждого приложения.

---

# 19. Icons

Одна icon library для всей платформы.

Стиль:

outline

stroke:

1.5–2

Размеры:

16
18
20
24px

Не использовать emoji вместо UI icons.

---

# 20. Motion

Hover:

120–160ms

Tooltip/dropdown:

150ms

Modal/drawer:

180–220ms

Easing:

ease-out

Не использовать:

* bounce;
* elastic;
* looping animation;
* decorative pulse animation everywhere.

---

# 21. Accessibility

WCAG AA.

Нельзя кодировать состояние только цветом.

Например:

ошибка должна иметь:

* цвет;
* icon;
* текст.

Focus states обязательны.

---

# 22. Запрет на Product Drift

Самое важное правило продуктовой системы.

BellenneEcho не является отдельным дизайнерским проектом.

BellenneVector не является отдельным дизайнерским проектом.

BellennePulse не является отдельным дизайнерским проектом.

Они являются продуктами системы Bellenne.

Поэтому запрещено создавать для отдельного продукта:

* новый UI kit;
* новую typography;
* новую card system;
* собственные radius;
* новую navbar;
* новый sidebar;
* отдельные dropdown styles;
* отдельные form styles;
* собственные shadows;
* дополнительную brand palette.

---

# 23. Подготовка к объединению приложений

Любая новая страница должна проектироваться так, чтобы в будущем её можно было перенести внутрь общего Bellenne application shell.

Избегать архитектуры:

App → уникальный layout → уникальная navigation → page

Предпочитать:

Bellenne Shell → Product Module → Page

---

# 24. Component Naming

Общие компоненты желательно называть без product prefix.

Хорошо:

Button
Card
DataTable
Modal
Select
Sidebar
FilterBar
MetricCard

Плохо:

EchoButton
PulseCard
VectorModal

Product-specific имя используется только если отличается сама бизнес-логика.

Например:

ReviewCard

CampaignMetric

MarketplaceOverview

---

# 25. Source of Truth

При конфликте:

1. Явная задача пользователя.
2. Этот BRANDBOOK.
3. Product specification.
4. Существующая Bellenne component system.
5. Минимально необходимое решение.

Consistency имеет больший приоритет, чем novelty.

---

# 26. Основной критерий

Пользователь должен иметь возможность переключиться:

BellennePulse → BellenneEcho → BellenneVector

и сразу понимать, что он всё ещё находится в одном продукте Bellenne.

Интерфейс меняет контекст.

Он не меняет дизайн-систему.
