# BellenneEcho Product Design Specification

## 1. Product

Official name:

BellenneEcho

Short module name:

Echo

Product category:

Marketplace Reputation & Review Automation

BellenneEcho предназначен для:

* получения отзывов;
* анализа отзывов;
* генерации ответов;
* автоматических ответов;
* ручной проверки AI-ответов;
* управления правилами автоматизации;
* контроля качества ответов;
* работы с рейтингом и репутацией.

---

# 2. Design System

BellenneEcho НЕ имеет собственной дизайн-системы.

Обязательный источник:

`docs/BRANDBOOK.md`

Все компоненты BellenneEcho должны использовать общую Bellenne Design System.

---

# 3. Product Accent

Echo Accent:

#06B6D4

Token:

```css
--product-accent: #06B6D4;
```

Название:

Echo Cyan

Accent означает принадлежность элемента к Echo.

Он НЕ означает positive/success.

---

# 4. Product Identity

В интерфейсе Echo Cyan допускается использовать для:

* Echo product icon;
* active Echo navigation;
* selected review;
* active filter;
* review analytics primary chart;
* AI processing indicator;
* small highlights;
* links внутри Echo;
* focus states;
* primary Echo-specific action.

Не использовать Echo Cyan как фон больших областей.

---

# 5. Основные сущности

Визуальная система должна учитывать следующие типы сущностей:

Review

Reply

AI Reply

Automation Rule

Marketplace

Product

Rating

Sentiment

Status

---

# 6. Review Card

Review Card должен использовать стандартную Bellenne Card.

Структура предпочтительно:

Marketplace / Product

Rating

Review text

Date / customer metadata

Reply status

Generated or published reply

Actions

---

# 7. Rating

Rating является отдельной бизнес-метрикой.

Для рейтинга допускаются звёзды.

Использование стандартного warning/accent gold:

#F59E0B

Пример:

★★★★★

Но рейтинг нельзя обозначать только цветом.

Числовое значение также должно быть доступно:

4.8

---

# 8. Sentiment

Sentiment является аналитической классификацией, а не декоративным цветом.

Positive:

#22D3A5

Negative:

#FF5C70

Neutral:

#AAB3C2

Mixed / uncertain:

#F59E0B

Всегда добавлять текстовый label.

---

# 9. Reply Status

Предпочтительные статусы:

Needs Reply

Generated

Waiting Approval

Scheduled

Published

Failed

Disabled

Не создавать случайные названия одного и того же состояния на разных страницах.

---

# 10. AI-generated content

AI-сгенерированный ответ должен явно отличаться статусом, но не отдельным стилем карточки.

Использовать:

AI icon

label:

AI Generated

или

Generated

Не использовать:

* rainbow gradients;
* magic purple everywhere;
* sparkles everywhere;
* отдельный AI UI-kit.

AI является функцией BellenneEcho, а не отдельным брендом.

---

# 11. Composer

Поле создания/редактирования ответа использует стандартные Bellenne form components.

Структура:

Review context

Reply editor

Character count при необходимости

AI generation action

Send / Approve action

---

# 12. Primary actions

Типичные Echo actions:

Generate Reply

Approve

Send

Publish

Regenerate

Edit

Ignore

Disable Automation

Primary action должен быть один.

Не делать все actions яркими.

---

# 13. Automatic Reply

Автоматизация должна визуально показывать:

Enabled / Disabled

Marketplace

Rules

Rating condition

Sentiment condition

Delay

Tone

Last execution

Errors

---

# 14. Automation state

Enabled:

success semantic color

Disabled:

neutral

Failed:

danger

Pending:

warning

Product Accent не заменяет status colors.

---

# 15. Reply Tone

Tone является настройкой, а не отдельной темой оформления.

Например:

Neutral

Friendly

Formal

Concise

Custom

Не изменять цвета интерфейса в зависимости от tone.

---

# 16. Review List

Для большого количества отзывов предпочтительна плотная list/table hybrid structure.

Не превращать каждый отзыв в огромную marketing card.

Пользователь должен быстро сканировать:

Rating

Marketplace

Product

Review

Reply status

Date

Automation status

---

# 17. Analytics

Echo analytics использует ту же систему графиков, что BellennePulse.

Primary Echo series:

#06B6D4

Допустимые метрики:

Total Reviews

Average Rating

Response Rate

Automatic Reply Rate

Average Response Time

Positive Sentiment

Negative Sentiment

AI Approval Rate

---

# 18. Empty states

Пример:

No reviews found

Измените фильтры или дождитесь синхронизации новых отзывов.

Не использовать большие иллюстрации.

---

# 19. Product Navigation

Внутри Echo предпочтительны разделы:

Overview

Reviews

Automation

Analytics

Templates

Settings

Это внутренняя навигация продукта.

Она не должна заменять общий Bellenne sidebar.

---

# 20. Future unified application

BellenneEcho должен проектироваться как модуль:

Bellenne Shell
→ Echo
→ Page

а не как независимое приложение:

Echo App
→ Echo-specific Shell
→ Page

---

# 21. Главный критерий Echo

BellenneEcho должен ощущаться как:

BellennePulse, созданный для работы с отзывами.

Не как отдельный SaaS-продукт другого производителя.
