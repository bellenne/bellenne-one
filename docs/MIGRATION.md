# Перенос существующих данных

Миграция выполняется отдельно от обычного запуска и никогда не стартует автоматически. Она заменяет базы в volumes BellenneOne, поэтому сначала создаётся резервная копия текущего состояния.

## Production Linux

Для production-сервера, на котором одновременно доступны старые контейнеры и новый проект BellenneOne, используйте Bash-скрипт:

```bash
chmod +x scripts/migrate-production-data.sh
./scripts/migrate-production-data.sh
```

Первая команда запускает только dry-run: проверяет Docker, Compose, имена старых контейнеров и наличие обязательных файлов.

Перед применением перенесите в production `.env` BellenneOne старые значения `APP_SECRET_KEY` и, если использовался, `CREDENTIALS_ENCRYPTION_KEY`. Скрипт сравнивает секреты контейнеров без вывода их значений.

Применение:

```bash
./scripts/migrate-production-data.sh --apply
```

По умолчанию ожидаются старые контейнеры:

* `bellennepulse`;
* `market-replies-web`;
* `wb-ads-statistics`.

Если production использует другие имена:

```bash
./scripts/migrate-production-data.sh \
  --old-pulse-container OLD_PULSE \
  --old-echo-container OLD_ECHO \
  --old-vector-container OLD_VECTOR
```

После успешной проверки добавьте к этой же команде `--apply`.

Скрипт останавливает старые приложения, проверяет SQLite WAL и целостность баз, сохраняет source snapshot и текущие volumes BellenneOne в `backups/production-migration-<дата>`, переносит базы и ключи Vector, восстанавливает владельцев, запускает новый стек и ожидает состояния `healthy`.

Старые контейнеры после успешной миграции остаются остановленными. Не удаляйте их и каталог резервной копии до полной проверки системы.

## До миграции

1. Остановите старые Docker-стеки Pulse, WBAnsewer и AdsStatistics, чтобы SQLite не записывала WAL во время копирования.
2. Создайте `.env` для BellenneOne.
3. Перенесите прежнее значение `APP_SECRET_KEY`. Если Pulse использовал явный `CREDENTIALS_ENCRYPTION_KEY`, перенесите и его.
4. Проверьте план миграции без изменений:

```powershell
.\scripts\migrate-existing-data.ps1
```

Скрипт по умолчанию ищет проекты в путях из исходной задачи:

- `D:\Works\Python\BellennePulse`;
- `D:\Works\Python\WBAnsewer`;
- `D:\Works\Python\AdsStatistics`.

Пути можно переопределить параметрами `-PulseRoot`, `-EchoRoot` и `-VectorRoot`.

## Применение

```powershell
.\scripts\migrate-existing-data.ps1 -Apply
```

Скрипт:

1. проверит обязательные базы Pulse и Echo;
2. откажется продолжать при непустом SQLite WAL;
3. остановит gateway и модули BellenneOne;
4. сохранит содержимое текущих volumes в `backups/migration-<дата>`;
5. перенесёт базы и ключи Vector;
6. восстановит владельцев файлов внутри контейнеров;
7. запустит BellenneOne и выполнит встроенные миграции схем.

Если в `AdsStatistics/data` нет `ads_statistics.db`, база Vector не переносится. Файлы `.token_key` и `.session_key` переносятся при наличии; `.token_key` обязателен для расшифровки уже сохранённого API-ключа.

## После миграции

1. Войдите с логином и паролем из прежнего Pulse.
2. Откройте `/settings` и последовательно проверьте каждый модуль.
3. В Echo убедитесь, что кабинеты, шаблоны и журнал принадлежат единому аккаунту.
4. В Vector проверьте маску API-ключа и выполните ручной тест сбора.
5. Не отключайте `ECHO_DRY_RUN`, пока ответы и диапазоны рейтингов не проверены.

Если в старой базе Echo было несколько пользователей, автоматическая привязка выполняется только по совпадающему имени. Это предотвращает случайное присвоение чужих кабинетов.
