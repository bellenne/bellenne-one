# Контракты marketplace API для Parity

Официальные источники проверены 01.10.2026. Ozon доступен через браузер;
прямой web fetch документации возвращает redirect loop.

- [WB: работа с товарами](https://dev.wildberries.ru/openapi/work-with-products)
- [WB: клубные цены](https://dev.wildberries.ru/release-notes?id=200)
- [Ozon: список товаров](https://docs.ozon.ru/api/seller/#operation/ProductAPI_GetProductList)
- [Ozon: сведения о товарах](https://docs.ozon.ru/api/seller/#operation/ProductAPI_GetProductInfoList)
- [Ozon: цены](https://docs.ozon.ru/api/seller/#operation/ProductAPI_GetProductInfoPrices)

## Wildberries

`POST https://content-api.wildberries.ru/content/v2/get/cards/list`:
`nmID` → external ID и SKU, `vendorCode` → seller article, `title` → имя.
Пагинация: cursor limit=100, updatedAt + nmID из ответа; filter.withPhoto=-1
для карточек и с фото, и без фото. Карточки из корзины этим методом не возвращаются.

`GET https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter`:
limit=1000, offset; соединение цен и карточек строго по nmID.

| Нормализованное поле | Источник | Значение |
|---|---|---|
| base_price | sizes[].price | Цена продавца до скидки |
| discount_price | sizes[].discountedPrice | Цена со скидкой продавца |
| loyalty_price | sizes[].clubDiscountedPrice | Цена с учётом скидки WB Клуба |
| currency | currencyIsoCode4217 | Валюта ответа |

Одна цена товара используется только если она предоставлена для всех размеров
и одинакова. Иначе соответствующий тип null, reason=size_prices_differ или
not_provided. Размерный вариант не выбирается произвольно. СПП/персональные
скидки покупателя и WB Кошелёк эти поля не гарантируют.

Токен Authorization с правами Контент (или соответствующими правами Продвижения)
и Цены и скидки. Проверка читает обе категории отдельно. Лимиты: Content —
100 запросов/минуту, Цены и скидки — 10/6 секунд, интервал 600 мс.
Адаптер выдерживает 600 мс между запросами; тесты и sync одного кабинета
сериализуются. Лимиты могут быть общими с другими сервисами того же продавца:
429 обрабатывается с учётом X-Ratelimit-Retry и Retry-After.

## Ozon

Заголовки Client-Id и Api-Key. Базовый URL `https://api-seller.ozon.ru`.

`POST /v3/product/list`: filter.visibility=ALL, limit=1000, last_id из предыдущего
ответа. Используются result.items[].product_id и offer_id.

`POST /v3/product/info/list`: пакет product_id не больше 1000; ответ items[].id,
sku, offer_id, name, statuses.status, is_archived/is_autoarchived.
product_id и sku хранятся отдельно. Неполный ответ информации о товарах
не считается полным каталогом и останавливает импорт этой площадки.

`POST /v5/product/info/prices`: filter.visibility=ALL, limit=1000, cursor.
Ответ items расположен на верхнем уровне, не result.items. Cursor используется
для следующей страницы; остановка при пустом cursor. Повторяющийся cursor
или повторяющиеся product_id вызывают ошибку контракта.

| Нормализованное поле | Источник | Значение |
|---|---|---|
| base_price | items[].price.price | Предельная цена без акций |
| discount_price | items[].price.marketing_seller_price | Предельная цена с акциями продавца, без дополнительной скидки Ozon |
| loyalty_price | отсутствует | null / unavailable: актуальный контракт не предоставляет цену Ozon Карты |
| currency | items[].price.currency_code | Валюта кабинета |

`old_price` — зачёркнутая справочная цена, не base_price. `min_price` — внутренний
нижний порог, не цена покупки. `net_price` — себестоимость. Ни одно из этих полей
не подставляется вместо отсутствующего типа цены. Устаревшее premium_price
не используется. price.price не подставляется вместо marketing_seller_price.

Текущая документация отдельно уточняет, что обе используемые цены — верхние
границы, а не гарантированная конечная цена покупателя. Это явно показано в UI.
Контракт не содержит метода получения Ozon Card price для данного импорта.

Обычный лимит при отсутствии отдельного ограничения — 50 запросов/секунду на
Client ID. Адаптер выдерживает 20 мс между запросами и обрабатывает 429.
Документация объявляет отключение total 23.11.2026 в пользу total_items:
адаптер не зависит от total и использует курсоры.

## Ошибки и эксплуатационные ограничения

Не повторяются 401/403 и прочие постоянные 4xx. Только read-запросы повторяются
при транспортном сбое, 429 и 5xx: до PARITY_RETRY_ATTEMPTS (по умолчанию 3),
backoff 1/2 секунды или Retry-After. Если Retry-After больше 60 секунд,
задание завершается безопасной ошибкой rate_limit, без раннего повторного запроса.
Права проверяются реальными read-запросами, не выводятся из валидности ключа.

JSON parse_float=Decimal, хранение денег — целые копейки через ExactDecimal.
Поля с более высокой точностью нормализуются до копейки; арифметика — Decimal.
Процент: (Ozon − WB) / WB × 100; деление на ноль даёт null. Валюты должны совпадать.

Общие rate limits с другими приложениями и отзыв/истечение API-ключа могут
приводить к ошибкам даже после успешной проверки. Лимиты, реальные права и
конкретные live payloads необходимо проверить после подключения кабинетов.
Контрактные тесты используют httpx.MockTransport; они не доказывают наличие
доступа к реальному аккаунту. Тестовый UI использует только синтетические товары.
