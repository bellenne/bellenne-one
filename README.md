# BellenneOne

BellenneOne объединяет пять самостоятельных продуктов за одним адресом и одной авторизацией:

- `/` — отдельная оболочка BellenneOne с описанием модулей;
- `/settings` — общий центр настроек;
- `/pulse/` — BellennePulse;
- `/echo/` — BellenneEcho;
- `/vector/` — BellenneVector;
- `/nest/` — BellenneNest, конфигурация и аудит API раскладки.
- `/proof/` — BellenneProof, оркестрация производственных заданий, Workers и доставки результатов.

Снаружи публикуется только nginx-шлюз. Shell и пять продуктовых контейнеров доступны только во внутренней Docker-сети.

## Запуск

1. Скопируйте `.env.example` в `.env`.
2. Замените `APP_SECRET_KEY` на длинную случайную строку.
3. Если переносите существующий Pulse, сохраните прежний `APP_SECRET_KEY` и `CREDENTIALS_ENCRYPTION_KEY`, иначе сохранённые API-ключи Pulse нельзя будет расшифровать.
4. Запустите стек:

```powershell
docker compose up -d --build
```

По умолчанию приложение откроется на [http://127.0.0.1:17863](http://127.0.0.1:17863). Порт меняется через `BELLENNE_PORT` в `.env`.

При чистом запуске перейдите на `/register` и создайте единый аккаунт. Для рабочего окружения после первичной проверки рекомендуется установить `DEMO_MODE=false`, `SEED_DEMO_DATA=false` и оставить `ECHO_DRY_RUN=true`, пока кабинеты и шаблоны Echo не проверены.

## Данные и настройки

Каждый модуль сохраняет свои бизнес-данные в отдельном Docker volume:

- `pulse_data` — аккаунт, кабинеты, планы и метрики Pulse;
- `echo_data` — кабинеты, шаблоны, очередь и журнал Echo;
- `vector_data` — рекламная статистика и ключ шифрования Vector.
- `nest_data` — пользовательские конфигурации Nest и история API-запросов.
- `proof_data` — очередь, события, настройки интеграций и неизменяемые результаты Proof.

Идентификатор единого аккаунта передаётся модулям через доверенные заголовки nginx. Прямой внешний доступ к контейнерам модулей не публикуется.

Для переноса существующих локальных баз сначала изучите [инструкцию миграции](docs/MIGRATION.md). Архитектура и границы сервисов описаны в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Проверка состояния

```powershell
docker compose ps
docker compose logs --tail 100 gateway shell pulse echo vector nest proof
```

Все семь сервисов должны иметь состояние `healthy`. Проверка шлюза доступна на `/gateway-health`.

## BellenneProof

Панель Proof находится по адресу `/proof/`. Входящие webhook amoCRM и Worker API доступны через gateway, но защищены отдельными секретами и не используют браузерную сессию:

```text
POST /proof/webhooks/amocrm/<индивидуальный webhook secret>
POST /proof/api/v1/workers/heartbeat
POST /proof/api/v1/jobs/claim
```

Каждый Worker получает собственный токен в Proof → Workers. Полное значение показывается один раз. Точные контракты и пример цикла mock Worker описаны в [apps/proof/README.md](apps/proof/README.md).

Для `BellenneProofWorker` на том же Docker-хосте используйте общую сеть `bellenneone_default` и адрес Core `http://proof:8000`. В репозитории Worker предусмотрен overlay `docker-compose.bellenne.yml`; внешний Worker подключается через публичный адрес вида `https://example.com/proof`. Суффикс `/api/v1` Worker добавляет самостоятельно.

В Proof → Интеграции подключение amoCRM разделено на четыре шага: OAuth-авторизация, webhook и три custom field, статусы сделки, затем Preset и включение. Для продакшена задайте `PROOF_PUBLIC_BASE_URL=https://one.customcraft-mes.ru`, а в amoCRM зарегистрируйте Redirect URI `https://one.customcraft-mes.ru/proof/integrations/amocrm/oauth/callback` и хук отключения `https://one.customcraft-mes.ru/proof/integrations/amocrm/oauth/revoked`. Интеграции нужны права на данные CRM и файлы.

Условия отправки webhook настраиваются только в amoCRM. Core получает событие, заново читает сделку через OAuth API и переносит в Job ровно три выбранных поля: полный UNC-путь заказа, номер макета и дополнительный идентификатор. Затем Core одним запросом переводит сделку в выбранный статус обработки и очищает выбранные в UI поля; только после успеха Job попадает в очередь Worker. После обработки Core загружает ZIP в сделку и переводит её в конечный статус. Соответствие UNC-префикса read-only mount настраивается отдельно для каждого Worker в Proof → Workers.

В Proof → Интеграции можно подключить Mattermost Incoming Webhook. Бот отправляет в выбранный канал только ошибки уровней Error и Critical; URL webhook хранится в зашифрованном виде.

## BellenneNest API

Совместимый endpoint исходного Layout API доступен через общий gateway:

```text
POST /nest/api/v1/calculate
AUTH-TOKEN: <персональный токен из раздела Nest → API>
X-MES-User: <пользователь MES, инициировавший расчёт>
```

Каждый аккаунт BellenneOne создаёт собственный токен на странице `/nest/api-info`. Полное значение показывается только сразу после создания или замены и хранится в базе только в виде SHA-256 digest. Токен автоматически определяет аккаунт, его настройки и доступную историю. Заголовок `X-MES-User` обязателен и сохраняет в журнале пользователя, который выполнил действие в MES; для совместимости также принимается `X-Bellenne-Username`.
