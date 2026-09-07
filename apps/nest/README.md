# BellenneNest

BellenneNest — API-first модуль BellenneOne для расчёта раскладки. Алгоритмическое ядро `app/core.py` и входная схема `app/schemas.py` перенесены из `D:\Works\Python\layout\api_version` без изменений.

## Маршруты

- `/nest/` — состояние модуля и последние обращения;
- `/nest/configuration` — реальные параметры `Settings`;
- `/nest/api-info` — endpoint и схема авторизации;
- `/nest/history` — журнал запросов и ответов;
- `POST /nest/api/v1/calculate` — совместимый внешний API;
- `GET /nest/settings/defaults` — исходные значения алгоритма.

## Идентификация API-запросов

`AUTH-TOKEN` обязателен и создаётся владельцем аккаунта в `/nest/api-info`. Каждый аккаунт BellenneOne имеет собственный токен; при замене предыдущий токен сразу становится недействительным. В базе сохраняется только SHA-256 digest, а полное значение отображается один раз.

- `X-MES-User` — обязательный логин или имя пользователя из авторизации MES, который инициировал расчёт;
- `X-Bellenne-Username` — совместимый alias для `X-MES-User`.

Владелец и его сохранённая конфигурация определяются по токену, поэтому передавать идентификатор аккаунта в API не требуется. Если тело не содержит объект `settings`, Nest применяет конфигурацию владельца токена. Явно переданные `settings` всегда имеют приоритет. В истории владельца отображается имя пользователя из MES.

## Тесты

```bash
docker build --target test -f apps/nest/Dockerfile -t bellennenest-test .
docker run --rm bellennenest-test
```
