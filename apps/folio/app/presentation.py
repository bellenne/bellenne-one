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
        "Результат отправки неизвестен",
        "Сначала синхронизируйте историю чата; не отправляйте сообщение повторно вслепую.",
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
        "Разрешите создание чатов только после проверки кабинета и статусов заказов.",
    ),
    "order_status_not_enabled": (
        "Статус заказа не разрешён для начала диалога",
        "Проверьте фактический статус заказа и правила автоматизации.",
    ),
    "worker_interrupted": (
        "Операция прервана перезапуском сервиса",
        "Безопасную операцию можно повторить. Отправку сначала необходимо сверить.",
    ),
    "worker_internal_error": (
        "Внутренняя ошибка обработки",
        "Повторите безопасную операцию. Если ошибка сохранится, проверьте журнал контейнера Folio Worker.",
    ),
    "send_internal_unknown": (
        "Результат отправки неизвестен",
        "Синхронизируйте историю чата и выполните ручную сверку.",
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
    "retailcrm_internal_unknown": (
        "Результат создания заказа RetailCRM неизвестен",
        "Проверьте заказ в RetailCRM по externalId перед повторными действиями.",
    ),
}


JOB_LABELS = {
    "probe": "Проверка подключения",
    "sync": "Синхронизация заказов и чатов",
    "start_chat": "Создание чата с покупателем",
    "retailcrm_probe": "Проверка подключения RetailCRM",
    "retailcrm_create": "Создание сделки в RetailCRM",
}

STATE_LABELS = {
    "pending": "Ожидает выполнения",
    "running": "Выполняется",
    "done": "Завершено",
    "failed": "Требует внимания",
    "unknown": "Результат необходимо сверить",
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
    "integration.queued_retailcrm_probe": "Запрошена проверка RetailCRM",
    "integration.done": "Операция интеграции завершена",
    "integration.failed": "Операция интеграции требует внимания",
    "integration.unknown": "Результат операции интеграции требует сверки",
    "integration.retry_requested": "Запрошен безопасный повтор операции",
    "order.imported": "Получен новый заказ Ozon",
    "order.changed": "Изменились данные заказа Ozon",
    "message.imported": "Получено сообщение Ozon",
    "message.duplicate": "Повторное сообщение Ozon распознано и пропущено",
    "chat.imported": "Получен новый чат Ozon",
    "event.processed": "Входящее сообщение обработано",
    "event.failed": "Входящее сообщение передано менеджеру из-за ошибки",
    "outbound.attempt": "Выполнена попытка отправки сообщения",
    "outbound.sent": "Ozon подтвердил отправку сообщения",
    "outbound.failed": "Ozon отклонил отправку сообщения",
    "outbound.unknown": "Результат отправки требует ручной сверки",
    "outbound.cancelled": "Отправка отменена после перехода в ручной режим",
    "outbound.reconciled": "Результат отправки сверен вручную",
    "outbound.retry_queued": "Запрошен безопасный повтор неуспешной отправки",
    "chat.linked": "Чат связан с товарной позицией",
    "media.added": "Добавлено изображение",
    "media.accepted": "Изображение добавлено в бриф",
    "mapping.updated": "Обновлено соответствие товара",
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
    title, action = ERRORS.get(
        code,
        (
            "Операция завершилась с ошибкой",
            "Повторите безопасную операцию. Если проблема сохранится, проверьте журнал Folio Worker.",
        ),
    )
    return {"title": title, "action": action}


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
