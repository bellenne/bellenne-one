"""Human-readable presentation for operational Folio states.

Internal codes remain in the database for stable diagnostics, but normal UI never
uses them as the only explanation.  This module deliberately contains no domain
decisions and can therefore be reused by templates and HTTP validation errors.
"""

from datetime import datetime


ERRORS = {
    "legacy_probe_configuration": (
        "Операция создана старой версией Folio",
        "Повторите проверку подключения: технические HTTP-поля больше не требуются.",
    ),
    "configure_sync_since": (
        "Не задано начало синхронизации",
        "Укажите дату, начиная с которой Folio должен загружать заказы Ozon.",
    ),
    "connection_failed": (
        "Не удалось подключиться к Ozon",
        "Проверьте доступ сервера к интернету и повторите проверку подключения.",
    ),
    "transport_failed": (
        "Ozon временно не отвечает",
        "Повторите безопасную операцию позже.",
    ),
    "transport_unknown": (
        "Folio проверяет отправку",
        "История чата будет проверена автоматически; повторной отправки сообщения не будет.",
    ),
    "read_timeout_unknown": (
        "Folio проверяет отправку",
        "Ozon не ответил вовремя. Folio сверит историю чата без повторной отправки.",
    ),
    "write_timeout_unknown": (
        "Folio проверяет отправку",
        "Ozon не ответил вовремя. Folio сверит историю чата без повторной отправки.",
    ),
    "send_internal_unknown": (
        "Folio проверяет отправку",
        "Сообщение могло дойти до Ozon. Folio проверит историю чата и продолжит сценарий после подтверждения.",
    ),
    "send_file_result_unknown": (
        "Отправка фото проверяется",
        "Ozon принял запрос без ID сообщения. Folio сверит изображение с историей чата; повторно отправлять его не нужно.",
    ),
    "outbound_file_missing": (
        "Фото для отправки недоступно",
        "Проверьте файл в чате и загрузите его повторно после исправления причины.",
    ),
    "retailcrm_order_missing": (
        "Сделка RetailCRM не найдена",
        "Проверьте создание сделки и её внешний ID в журнале Folio.",
    ),
    "retailcrm_items_unverified": (
        "Не удалось безопасно изменить сделку RetailCRM",
        "Проверьте состав сделки в RetailCRM: Folio не будет рисковать удалением товарных позиций.",
    ),
    "retailcrm_status_unavailable": (
        "Статус RetailCRM больше не доступен",
        "Обновите список статусов в настройках RetailCRM и выберите действующий статус в сценарии.",
    ),
    "retailcrm_statuses_unavailable": (
        "Не удалось получить статусы RetailCRM",
        "Проверьте право чтения справочников и повторите проверку подключения.",
    ),
    "mattermost_not_configured": (
        "Mattermost не настроен",
        "Сохраните входящий webhook в настройках Folio.",
    ),
    "mattermost_credentials_unavailable": (
        "Webhook Mattermost недоступен",
        "Повторно сохраните входящий webhook в настройках Folio.",
    ),
    "mattermost_template_invalid": (
        "Не удалось подготовить сообщение Mattermost",
        "Проверьте переменные в узле сценария.",
    ),
    "mattermost_message_empty": (
        "Сообщение Mattermost пустое",
        "Заполните текст узла сценария.",
    ),
    "mattermost_transport_unknown": (
        "Результат отправки в Mattermost неизвестен",
        "Проверьте чат Mattermost перед повторной отправкой.",
    ),
    "mattermost_internal_unknown": (
        "Результат отправки в Mattermost неизвестен",
        "Проверьте чат Mattermost и журнал Folio; автоматического повтора не будет.",
    ),
    "invalid_response": (
        "Ozon вернул непонятный ответ",
        "Повторите проверку. Если ошибка сохранится, откройте технические детали операции.",
    ),
    "unexpected_http_status": (
        "Ozon вернул неожиданный статус",
        "Повторите проверку подключения позже.",
    ),
    "chat_api_not_enabled": (
        "Отправка сообщений не включена",
        "Проверьте доступ к чатам и явно разрешите отправку в настройках Folio.",
    ),
    "chat_start_not_enabled": (
        "Создание чатов не включено",
        "Проверьте доступ кабинета и включите автоматическое сопровождение новых заказов.",
    ),
    "chat_start_not_verified": (
        "Права кабинета не подтверждены",
        "Проверьте доступ к заказам, чатам, вложениям и отправке сообщений в настройках Ozon.",
    ),
    "start_item_ambiguous": (
        "Нельзя однозначно выбрать товар для чата",
        "У отправления должна быть ровно одна позиция с настроенным артикулом и опубликованным сценарием.",
    ),
    "start_returned_existing_chat": (
        "Диалог для отправления уже запущен",
        "Обновите страницу. Если заказ не появился в чатах Folio, проверьте журнал запуска.",
    ),
    "order_before_automation": (
        "Заказ оформлен до включения автоматизации",
        "Folio не начинает задним числом чаты по старым заказам.",
    ),
    "order_cancelled": (
        "Заказ отменён в Ozon",
        "Чат по отменённому заказу не начинается.",
    ),
    "worker_interrupted": (
        "Операция прервана перезапуском сервиса",
        "Безопасную операцию можно повторить. Отправку сначала необходимо сверить.",
    ),
    "worker_internal_error": (
        "Внутренняя ошибка обработки",
        "Повторите безопасную операцию. Если ошибка сохранится, проверьте журнал контейнера Folio Worker.",
    ),
    "event_processing_failed": (
        "Не удалось обработать входящее сообщение",
        "Чат передан менеджеру, чтобы данные покупателя не потерялись.",
    ),
    "invalid_order_event": (
        "Заказ Ozon имеет неподдерживаемый формат",
        "Проверьте актуальность API-контракта Ozon.",
    ),
    "invalid_order_item": (
        "Товарная позиция Ozon имеет неполные данные",
        "Проверьте offer_id, SKU и количество в ответе Ozon.",
    ),
    "invalid_chat_event": (
        "Чат Ozon не содержит идентификатор",
        "Проверьте актуальность API-контракта Ozon.",
    ),
    "invalid_message_event": (
        "Сообщение Ozon не содержит идентификатор",
        "Сообщение не обработано, чтобы не создать дубль.",
    ),
    "seller_info_contract_changed": (
        "Ozon вернул неподдерживаемые данные кабинета",
        "Проверьте актуальную версию Seller API перед повтором.",
    ),
    "orders_contract_changed": (
        "Изменился формат заказов Ozon",
        "Синхронизация остановлена без изменения сохранённых заказов.",
    ),
    "orders_cursor_stalled": (
        "Ozon не продолжил выдачу заказов",
        "Синхронизация остановлена, чтобы не зациклить запросы.",
    ),
    "product_list_contract_changed": (
        "Изменился формат списка товаров Ozon",
        "Обновление каталога остановлено; проверьте актуальную версию Seller API.",
    ),
    "product_cursor_stalled": (
        "Ozon не продолжил выдачу товаров",
        "Обновление каталога остановлено, чтобы не сохранить неполный список.",
    ),
    "chat_list_contract_changed": (
        "Изменился формат списка чатов Ozon",
        "Проверьте актуальную версию Chat API перед повтором.",
    ),
    "chat_history_contract_changed": (
        "Изменился формат истории чата Ozon",
        "История не импортирована, чтобы не потерять сообщения.",
    ),
    "chat_cursor_stalled": (
        "Ozon не продолжил выдачу чатов",
        "Синхронизация остановлена, чтобы не зациклить запросы.",
    ),
    "history_cursor_stalled": (
        "Ozon не продолжил историю сообщений",
        "Синхронизация остановлена, чтобы не создать дубли.",
    ),
    "message_data_contract_changed": (
        "Сообщение Ozon имеет неподдерживаемый формат",
        "Чат передан менеджеру; проверьте актуальность Chat API.",
    ),
    "send_result_unknown": (
        "Ozon не подтвердил отправку сообщения",
        "Синхронизируйте историю чата перед любым повтором.",
    ),
    "start_result_unknown": (
        "Ozon не подтвердил создание чата",
        "Проверьте список чатов и не создавайте диалог повторно вслепую.",
    ),
    "retailcrm_not_configured": (
        "RetailCRM не подключена",
        "Сохраните адрес аккаунта, код магазина и API-ключ в настройках Folio.",
    ),
    "retailcrm_credentials_unavailable": (
        "Ключ RetailCRM недоступен",
        "Повторно сохраните API-ключ RetailCRM.",
    ),
    "retailcrm_not_verified": (
        "Права RetailCRM не подтверждены",
        "Проверьте подключение и права чтения и создания заказов.",
    ),
    "retailcrm_missing_order_read": (
        "Нет права читать заказы RetailCRM",
        "Добавьте API-ключу право order_read и повторите проверку.",
    ),
    "retailcrm_missing_order_write": (
        "Нет права создавать заказы RetailCRM",
        "Добавьте API-ключу право order_write и повторите проверку.",
    ),
    "retailcrm_missing_site": (
        "Нет доступа к выбранному магазину RetailCRM",
        "Проверьте код магазина и доступ API-ключа к нему.",
    ),
    "retailcrm_transport_unknown": (
        "Результат запроса к RetailCRM неизвестен",
        "Folio остановил сценарий и не будет создавать заказ повторно без проверки по externalId.",
    ),
    "retailcrm_transport_failed": (
        "Не удалось подключиться к RetailCRM",
        "Создание сделки не начиналось. Проверьте сеть и доступность RetailCRM.",
    ),
    "retailcrm_invalid_response": (
        "RetailCRM вернула неподдерживаемый ответ",
        "Проверьте журнал операции и актуальность API RetailCRM.",
    ),
    "retailcrm_instance_missing": (
        "Бриф для RetailCRM не найден",
        "Откройте связанный чат и передайте обработку менеджеру.",
    ),
    "retailcrm_template_invalid": (
        "Комментарий RetailCRM не удалось сформировать",
        "Проверьте переменные комментария в опубликованном сценарии.",
    ),
    "retailcrm_public_base_url_missing": (
        "Адрес Folio для ссылок на фото не настроен",
        "Укажите FOLIO_PUBLIC_BASE_URL — внешний адрес BellenneOne. Эта сделка не будет создана повторно автоматически.",
    ),
    "retailcrm_public_base_url_invalid": (
        "Адрес Folio для ссылок на фото некорректен",
        "Проверьте FOLIO_PUBLIC_BASE_URL: нужен HTTPS-адрес BellenneOne без пути.",
    ),
    "retailcrm_media_missing": (
        "Фото для передачи в RetailCRM недоступно",
        "Проверьте вложение в чате Folio и передайте заказ менеджеру.",
    ),
    "retailcrm_internal_unknown": (
        "Результат создания заказа RetailCRM неизвестен",
        "Проверьте заказ в RetailCRM по externalId перед повторными действиями.",
    ),
}


JOB_LABELS = {
    "probe": "Проверка подключения",
    "sync": "Синхронизация заказов и чатов",
    "catalog_sync": "Обновление товаров Ozon",
    "start_chat": "Создание чата с покупателем",
    "retailcrm_probe": "Проверка подключения RetailCRM",
    "retailcrm_create": "Создание сделки в RetailCRM",
    "retailcrm_note": "Запись результата в RetailCRM",
    "mattermost_send": "Отправка в Mattermost",
}

STATE_LABELS = {
    "pending": "Ожидает выполнения",
    "running": "Выполняется",
    "done": "Завершено",
    "failed": "Требует внимания",
    "unknown": "Результат не подтверждён",
    "sent": "Отправлено",
    "cancelled": "Закрыто без повтора",
    "processed": "Обработано",
}

ROLE_LABELS = {"admin": "Администратор", "manager": "Менеджер"}
ACTOR_LABELS = {
    "buyer": "Покупатель",
    "seller": "Продавец",
    "bot": "Бот Folio",
    "manager": "Менеджер",
    "external": "Внешний участник",
    "system": "Система",
}

INSTANCE_LABELS = {
    "new": "Новый бриф",
    "collecting": "Сбор данных",
    "waiting_reply": "Ожидается ответ покупателя",
    "waiting_integration": "Создание сделки в RetailCRM",
    "waiting_mockup": "Ожидает макет от менеджера",
    "waiting_approval": "Ожидает ответа покупателя о макете",
    "needs_manager": "Требуется менеджер",
    "needs_review": "Бриф ожидает проверки",
    "waiting_release": "Проверен, ожидает передачи",
    "ready": "Готов к передаче",
    "closed": "Завершён",
}

FIELD_LABELS = {
    "photos": "Фотографии",
    "background": "Пожелания к фону",
    "caption": "Текст для коллажа",
    "wishes": "Дополнительные пожелания",
    "template_id": "Шаблон оформления",
}

OBJECT_LABELS = {
    "settings": "настройки",
    "account": "кабинет",
    "user": "пользователь",
    "job": "операция",
    "posting": "отправление",
    "item": "товарная позиция",
    "message": "сообщение",
    "event": "входящее событие",
    "outbox": "исходящее сообщение",
    "media": "изображение",
    "mapping": "привязка товара",
    "template": "шаблон",
    "scenario": "сценарий",
    "version": "версия сценария",
    "instance": "бриф",
    "chat": "чат",
    "retailcrm_integration": "подключение RetailCRM",
    "retailcrm_action": "создание сделки RetailCRM",
    "mattermost_action": "сообщение Mattermost",
}

AUDIT_LABELS = {
    "settings.updated": "Изменены настройки Folio",
    "settings.ozon_updated": "Изменены настройки Ozon",
    "settings.automation_updated": "Изменены правила автоматизации",
    "settings.retailcrm_updated": "Изменено подключение RetailCRM",
    "account.saved": "Сохранено подключение Ozon",
    "user.updated": "Изменён доступ пользователя",
    "integration.queued_probe": "Запрошена проверка подключения",
    "integration.queued_sync": "Запрошена синхронизация",
    "catalog.refresh_queued": "Запрошено обновление товаров Ozon",
    "integration.queued_retailcrm_probe": "Запрошена проверка RetailCRM",
    "integration.done": "Операция интеграции завершена",
    "integration.failed": "Операция интеграции требует внимания",
    "integration.unknown": "Результат операции интеграции требует сверки",
    "integration.retry_requested": "Запрошен безопасный повтор операции",
    "integration.cancelled": "Попытка создания чата закрыта: заказ отменён",
    "order.imported": "Получен новый заказ Ozon",
    "order.changed": "Изменились данные заказа Ozon",
    "order.cancelled": "Заказ отменён в Ozon, сценарий остановлен",
    "message.imported": "Получено сообщение Ozon",
    "message.duplicate": "Повторное сообщение Ozon распознано и пропущено",
    "chat.imported": "Получен новый чат Ozon",
    "event.processed": "Входящее сообщение обработано",
    "event.ignored_cancelled": "Сообщение сохранено без ответа: заказ отменён",
    "event.failed": "Входящее сообщение передано менеджеру из-за ошибки",
    "outbound.attempt": "Выполнена попытка отправки сообщения",
    "outbound.sent": "Ozon подтвердил отправку сообщения",
    "outbound.failed": "Ozon отклонил отправку сообщения",
    "outbound.unknown": "Folio проверяет результат отправки по истории Ozon",
    "outbound.cancelled": "Отправка отменена после перехода в ручной режим",
    "outbound.reconciled": "Результат отправки сверен вручную",
    "outbound.reconciled_auto": "Folio подтвердил отправку по истории Ozon",
    "outbound.retry_queued": "Запрошен безопасный повтор неуспешной отправки",
    "chat.linked": "Чат связан с товарной позицией",
    "media.added": "Добавлено изображение",
    "media.accepted": "Изображение добавлено в бриф",
    "mapping.updated": "Обновлено соответствие товара",
    "mapping.deleted": "Удалена привязка товара",
    "template.saved": "Сохранён шаблон оформления",
    "scenario.created": "Создан черновик сценария",
    "scenario.save": "Сохранён черновик сценария",
    "scenario.validate": "Сценарий проверен",
    "scenario.preview": "Выполнена симуляция сценария",
    "scenario.publish": "Опубликована версия сценария",
    "scenario.published": "Опубликована версия сценария",
    "scenario.started": "Запущен сценарий для товарной позиции",
    "scenario.collecting": "Сценарий продолжил сбор данных",
    "scenario.waiting_reply": "Сценарий ожидает ответ покупателя",
    "scenario.needs_manager": "Сценарий передан менеджеру",
    "scenario.needs_review": "Бриф передан на проверку",
    "scenario.waiting_release": "Бриф ожидает разрешённого времени передачи",
    "scenario.ready": "Бриф готов к передаче",
    "scenario.closed": "Сценарий завершён",
    "scenario.rollback": "Опубликован возврат к выбранной версии",
    "scenario.copy": "Создана копия сценария",
    "answer.rejected": "Ответ покупателя не прошёл проверку шага",
    "answer.accepted": "Ответ покупателя принят сценарием",
    "chat.takeover": "Чат переведён в ручной режим",
    "chat.resumed": "Чат возвращён боту",
    "chat.autorecovered": "Бот продолжил диалог после подтверждения отправки",
    "brief.reviewed": "Бриф проверен менеджером",
    "brief.ready": "Бриф готов к передаче",
    "brief.needs_manager": "Бриф возвращён менеджеру",
    "brief.exported": "Экспортирован готовый бриф",
    "retailcrm.queued": "Создание сделки RetailCRM поставлено в очередь",
    "retailcrm.sent": "RetailCRM подтвердила создание сделки",
    "retailcrm.failed": "RetailCRM отклонила создание сделки",
    "retailcrm.unknown": "Результат создания сделки RetailCRM требует сверки",
    "retailcrm.simulated": "Создание сделки RetailCRM проверено в тестовом прогоне",
}


def error_details(code):
    if not code:
        return {"title": "Без ошибок", "action": ""}
    if code.startswith("ozon_http_"):
        status = code.removeprefix("ozon_http_")
        if status in {"401", "403"}:
            return {
                "title": "Ozon отклонил доступ",
                "action": "Проверьте Client ID, API-ключ и права кабинета.",
            }
        if status == "429":
            return {
                "title": "Ozon временно ограничил частоту запросов",
                "action": "Folio дождётся разрешённого времени; не запускайте повтор вручную.",
            }
        if status.startswith("5"):
            return {
                "title": "Сервис Ozon временно недоступен",
                "action": "Повторите безопасную операцию позже.",
            }
        return {
            "title": f"Ozon отклонил запрос (HTTP {status})",
            "action": "Проверьте права кабинета и актуальность операции.",
        }
    if code.startswith("retailcrm_http_"):
        status = code.removeprefix("retailcrm_http_")
        if status in {"401", "403"}:
            return {
                "title": "RetailCRM отклонила доступ",
                "action": "Проверьте API-ключ, его права и доступ к выбранному магазину.",
            }
        if status == "429":
            return {
                "title": "RetailCRM ограничила частоту запросов",
                "action": "Подождите и повторите только заведомо неуспешную операцию.",
            }
        if status.startswith("5"):
            return {
                "title": "RetailCRM временно недоступна",
                "action": "Перед повтором проверьте наличие заказа по externalId.",
            }
        return {
            "title": f"RetailCRM отклонила запрос (HTTP {status})",
            "action": "Проверьте поля шага, API-ключ и настройки магазина.",
        }
    if code.startswith("mattermost_http_"):
        status = code.removeprefix("mattermost_http_")
        return {
            "title": f"Mattermost отклонил сообщение (HTTP {status})",
            "action": "Проверьте входящий webhook и права канала Mattermost. При HTTP 5xx проверьте чат перед повтором.",
        }
    title, action = ERRORS.get(
        code,
        (
            "Операция завершилась с ошибкой",
            "Повторите безопасную операцию. Если проблема сохранится, проверьте журнал Folio Worker.",
        ),
    )
    return {"title": title, "action": action}


def operation_error_details(kind, state, code):
    if kind == "start_chat" and state == "cancelled":
        return {
            "title": "Заказ отменён",
            "action": "Попытка запуска чата закрыта без повторного запроса к Ozon.",
        }
    if kind == "start_chat" and state == "unknown":
        return {
            "title": "Создание чата не подтверждено",
            "action": (
                "Folio не повторяет запрос к Ozon, чтобы не создать дубликат. "
                "Неопределённый результат сохранён в журнале."
            ),
        }
    return error_details(code)


def label(value, labels):
    return labels.get(value, "Неизвестное состояние")


def actor_label(value):
    if value in ACTOR_LABELS:
        return ACTOR_LABELS[value]
    if str(value).startswith("manager:"):
        return "Менеджер"
    if str(value).isdigit():
        return f"Пользователь Bellenne №{value}"
    return "Участник Folio"


def format_time(value):
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return "—"
