"""A deliberately finite, validated graph interpreter; graphs and text live in DB."""

import json
import math
import re
import string
from datetime import datetime, timedelta

from . import db, image_tasks, policy, retailcrm

TYPES = {
    "portrait_background": "Картина + фон",
    "collage": "Коллаж",
    "template_art": "По шаблону",
}
KINDS = {
    "start": "Начало",
    "send": "Сообщение",
    "ask_text": "Запрос текста",
    "ask_photo": "Запрос фото",
    "ask_input": "Текст или фото",
    "choice": "Выбор",
    "condition": "Условие",
    "wait": "Ожидание ответа",
    "handoff": "Менеджеру",
    "confirm": "Подтверждение покупателем",
    "retailcrm": "Создать сделку в RetailCRM",
    "await_mockup": "Ожидать макет от менеджера",
    "approval": "Согласование макета",
    "retailcrm_note": "Записать итог в RetailCRM",
    "mattermost": "Сообщение в Mattermost",
    "image_worker": "Создать макет через воркер",
    "ready": "Проверка брифа",
    "end": "Конец",
}
FIELDS = {
    "portrait_background": {"photos", "background", "wishes"},
    "collage": {"photos", "caption", "wishes"},
    "template_art": {"photos", "template_id", "wishes"},
}
FIELD_LABELS = {
    "photos": "Фотографии",
    "background": "Пожелания к фону",
    "caption": "Текст для коллажа",
    "wishes": "Дополнительные пожелания",
    "template_id": "Выбранный шаблон",
}
WAITING = {"ask_text", "ask_photo", "ask_input", "choice", "wait", "confirm", "await_mockup", "approval"}
TEXT_KINDS = (WAITING - {"await_mockup"}) | {"send"}
RETAILCRM_VARIABLES = {
    "posting",
    "sku",
    "offer_id",
    "product_name",
    "quantity",
    "summary",
    "approval_outcome",
    "integration_error",
}
IMAGE_PROMPT_VARIABLES = {
    "posting", "offer_id", "product_name", "quantity", "summary",
    "background", "caption", "wishes", "template_id",
}
# Keep dictionary versions immutable: a published scenario retains its answer
# semantics even if later deployments add a new dictionary version.
APPROVAL_DICTIONARIES = {
    1: {
        "accept": {
            "да", "ага", "угу", "подтверждаю", "подтверждаю макет",
            "да подтверждаю", "согласен", "согласна", "согласен с макетом",
            "согласна с макетом", "согласовываю", "макет согласован",
            "утверждаю", "утверждаю макет", "макет утверждаю", "одобряю",
            "одобрено", "принимаю", "все устраивает", "всё устраивает",
            "меня все устраивает", "меня всё устраивает", "все подходит",
            "всё подходит", "макет подходит", "макет устраивает",
            "да макет подходит", "да все устраивает", "да всё устраивает",
            "ок", "окей", "ok", "хорошо", "отлично", "все хорошо",
            "всё хорошо", "можно печатать", "можно в печать",
            "отправляйте в печать", "отправляйте на печать", "подтверждено",
        },
        "reject": {
            "нет", "нет не подходит", "нет нужны правки", "нет нужна правка",
            "не подтверждаю", "пока не подтверждаю", "не согласен",
            "не согласна", "не согласовываю", "не утверждаю", "отклоняю",
            "не устраивает", "меня не устраивает", "не подходит",
            "макет не подходит", "макет не устраивает", "не одобряю",
            "не принимаю", "нужны правки", "нужна правка", "нужно исправить",
            "надо исправить", "нужно поменять", "нужно изменить",
            "нужно доработать", "исправьте", "измените", "поменяйте",
            "переделать", "переделайте", "переделайте макет",
            "не нравится", "макет не нравится", "отказываюсь",
        },
    },
}
APPROVAL_LEGACY = {
    "accept": {
        "да", "ага", "угу", "подтверждаю", "согласен", "согласна", "утверждаю",
        "одобряю", "принимаю", "все устраивает", "всё устраивает", "все подходит",
        "всё подходит", "макет подходит", "макет устраивает", "ок", "окей", "ok",
        "хорошо", "отлично", "можно печатать", "отправляйте в печать", "подтверждено",
    },
    "reject": {
        "нет", "не подтверждаю", "не согласен", "не согласна", "не устраивает",
        "не подходит", "макет не подходит", "макет не устраивает", "не одобряю",
        "не принимаю", "нужны правки", "нужна правка", "нужно исправить",
        "исправьте", "переделать", "переделайте", "не нравится", "отказываюсь",
    },
}


def approval_phrase(value):
    """Only exact normalized phrases; mixed/qualified replies need a manager."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.casefold().replace("ё", "е"))).strip()


def approval_variants(node, key):
    version = node.get("dictionary_version")
    if version is not None:
        dictionary = APPROVAL_DICTIONARIES.get(version)
        if dictionary is None:
            raise Invalid("Неподдерживаемая версия словаря согласования")
        values = dictionary[key]
    elif node.get("use_dictionary") is True:
        # Already published graphs keep their original built-ins and custom phrases.
        values = (*APPROVAL_LEGACY[key], *str(node.get(key) or "").splitlines())
    else:
        values = str(node.get(key) or "").splitlines()
    return {phrase for value in values if (phrase := approval_phrase(value))}


class Invalid(ValueError):
    pass


TRANSFER_FORMAT = "bellenne-folio-scenario"
TRANSFER_VERSION = 1
TRANSFER_LIMIT = 256 * 1024


def export_package(name, product_type, graph):
    return {
        "format": TRANSFER_FORMAT,
        "version": TRANSFER_VERSION,
        "name": name,
        "product_type": product_type,
        "graph": graph,
    }


def import_package(raw: bytes):
    if not raw or len(raw) > TRANSFER_LIMIT:
        raise Invalid("Файл сценария пуст или превышает 256 КБ")
    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    try:
        package = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Invalid("Файл сценария должен быть корректным JSON в UTF-8") from exc
    if not isinstance(package, dict) or package.get("format") != TRANSFER_FORMAT or package.get("version") != TRANSFER_VERSION:
        raise Invalid("Неподдерживаемый формат или версия файла сценария")
    name, product_type, graph = package.get("name"), package.get("product_type"), package.get("graph")
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
            or not isinstance(product_type, str) or product_type not in TYPES):
        raise Invalid("Проверьте название и тип товара в файле сценария")
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not 1 <= len(graph["nodes"]) <= 100:
        raise Invalid("В файле нет корректной схемы шагов")
    ids = set()
    for node in graph["nodes"]:
        if not isinstance(node, dict):
            raise Invalid("Один из шагов сценария повреждён")
        node_id = node.get("id")
        kind = node.get("kind")
        if (not isinstance(node_id, str) or not node_id or len(node_id) > 100
                or not node_id.replace("_", "").isalnum() or node_id in ids
                or not isinstance(kind, str) or kind not in KINDS):
            raise Invalid("В файле есть неизвестный шаг или повторяющийся ID")
        ids.add(node_id)
        if any(not isinstance(node[key], str) for key in (
            "title", "text", "field", "accept", "equals", "comment", "status",
            "order_type", "order_method", "message", "prompt",
        ) if key in node):
            raise Invalid("Текстовое поле шага в файле повреждено")
        if "choices" in node and (
            not isinstance(node["choices"], list)
            or any(not isinstance(choice, str) for choice in node["choices"])
        ):
            raise Invalid("Варианты выбора в файле повреждены")
        if "required" in node and not isinstance(node["required"], bool):
            raise Invalid("Признак обязательного ответа в файле повреждён")
        if any(node[key] is not None and not isinstance(node[key], (int, float))
               for key in ("min", "max", "max_length", "hours") if key in node):
            raise Invalid("Числовое поле шага в файле повреждено")
        position = node.get("position")
        if position is not None and (
            not isinstance(position, dict)
            or any(not isinstance(position.get(axis), (int, float))
                   or not math.isfinite(position[axis])
                   or abs(position[axis]) > 1_000_000 for axis in ("x", "y"))
        ):
            raise Invalid("Координаты шага в файле повреждены")
        if any(not isinstance(node.get(key), str) for key in ("next", "otherwise", "error") if key in node):
            raise Invalid("Переход шага в файле повреждён")
    normalize(graph, product_type)
    return name.strip(), product_type, graph


def normalize(graph, product_type):
    """Apply product invariants that are not administrator-facing settings."""
    changed = False
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    for node in nodes:
        if node.get("kind") == "approval":
            if node.get("dictionary_version") != 1:
                node["dictionary_version"] = 1
                changed = True
            for old_setting in ("use_dictionary", "accept", "reject"):
                if old_setting in node:
                    del node[old_setting]
                    changed = True
    photo_nodes = [
        node
        for node in nodes
        if node.get("kind") in {"ask_photo", "ask_input"}
    ]
    if photo_nodes and not any(node.get("required") for node in photo_nodes):
        photo_nodes[0]["required"] = True
        changed = True
    if product_type == "template_art":
        template_nodes = [
            node
            for node in nodes
            if node.get("kind") == "choice" and node.get("field") == "template_id"
        ]
        if template_nodes and not any(node.get("required") for node in template_nodes):
            template_nodes[0]["required"] = True
            changed = True
    return changed


def initial_graph(product_type):
    """Unpublished skeletons: the administrator must supply every buyer-facing text."""
    nodes = [
        {"id": "start", "kind": "start", "next": "photos", "position": {"x": 80, "y": 180}},
        {
            "id": "photos",
            "kind": "ask_photo",
            "field": "photos",
            "required": True,
            "text": "",
            "min": None,
            "max": 8 if product_type == "collage" else None,
            "next": "details",
            "position": {"x": 360, "y": 180},
        },
    ]
    field = {
        "portrait_background": "background",
        "collage": "caption",
        "template_art": "template_id",
    }[product_type]
    nodes += [
        {
            "id": "details",
            "kind": "choice" if field == "template_id" else "ask_text",
            "field": field,
            "required": True,
            "text": "",
            "choices": [],
            "next": "ready",
            "position": {"x": 640, "y": 180},
        },
        {"id": "ready", "kind": "ready", "position": {"x": 920, "y": 180}},
    ]
    return {"nodes": nodes}


def validate(graph, product_type):
    if product_type not in TYPES:
        raise Invalid("Неизвестный тип товара")
    nodes = graph.get("nodes", [])
    if not nodes or len(nodes) > 100:
        raise Invalid("Сценарий должен содержать от 1 до 100 шагов")
    index = {n.get("id"): n for n in nodes}
    if len(index) != len(nodes) or any(
        not isinstance(k, str) or not k or not k.replace("_", "").isalnum()
        for k in index
    ):
        raise Invalid(
            "ID шагов должны быть непустыми и уникальными: буквы, цифры, подчёркивание"
        )
    starts = [n for n in nodes if n.get("kind") == "start"]
    if len(starts) != 1:
        raise Invalid("Нужен ровно один шаг Начало")
    fields = {
        n.get("field")
        for n in nodes
        if n.get("kind") in {"ask_text", "ask_photo", "ask_input", "choice"}
    }
    if any(n.get("kind") == "ask_input" for n in nodes):
        fields.add("photos")
    required = {
        "photos" if n.get("kind") == "ask_input" else n.get("field")
        for n in nodes if n.get("required")
    }
    if "photos" not in fields:
        raise Invalid("Добавьте шаг «Запросить фото»: без исходного фото бриф нельзя проверить")
    if "photos" not in required:
        raise Invalid("Сделайте шаг «Запросить фото» обязательной частью брифа")
    if product_type == "template_art" and "template_id" not in fields:
        raise Invalid("Добавьте шаг «Предложить выбор шаблона»")
    if product_type == "template_art" and "template_id" not in required:
        raise Invalid("Выбор шаблона должен быть обязательной частью брифа")
    if not any(n.get("kind") in {"ready", "approval"} for n in nodes):
        raise Invalid("Нужен шаг проверки брифа")
    requires_confirmation = any(n.get("kind") == "confirm" for n in nodes)
    for node in nodes:
        kind = node.get("kind")
        if kind not in KINDS:
            raise Invalid("Неподдерживаемый тип шага")
        if kind in TEXT_KINDS:
            if not node.get("text", "").strip():
                raise Invalid(f"Заполните текст шага {node['id']}")
            try:
                for _, variable, spec, conversion in string.Formatter().parse(
                    node["text"]
                ):
                    if variable is not None and (
                        variable
                        not in fields | {"posting", "offer_id", "summary", "templates"}
                        or spec
                        or conversion
                    ):
                        raise Invalid(
                            "Неизвестная переменная или форматирование в тексте"
                        )
            except ValueError as exc:
                raise Invalid("Некорректные фигурные скобки в тексте") from exc
        if kind in {"retailcrm", "retailcrm_note"}:
            if not str(node.get("comment") or "").strip():
                raise Invalid(
                    f"Заполните комментарий для сделки в шаге {node['id']}"
                )
            try:
                for _, variable, spec, conversion in string.Formatter().parse(
                    node["comment"]
                ):
                    if variable is not None and (
                        variable not in fields | RETAILCRM_VARIABLES
                        or spec
                        or conversion
                    ):
                        raise Invalid(
                            "В комментарии RetailCRM используется неизвестная переменная"
                        )
            except ValueError as exc:
                raise Invalid(
                    "Некорректные фигурные скобки в комментарии RetailCRM"
                ) from exc
            if not node.get("error"):
                raise Invalid(
                    f"Укажите переход при ошибке RetailCRM для {node['id']}"
                )
            status_code = str(node.get("status") or "").strip()
            if status_code and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", status_code):
                raise Invalid("Некорректный символьный код статуса RetailCRM")
        if kind == "mattermost":
            if not str(node.get("message") or "").strip():
                raise Invalid(f"Заполните сообщение Mattermost в шаге {node['id']}")
            try:
                for _, variable, spec, conversion in string.Formatter().parse(node["message"]):
                    if variable is not None and (variable not in fields | RETAILCRM_VARIABLES or spec or conversion):
                        raise Invalid("В сообщении Mattermost используется неизвестная переменная")
            except ValueError as exc:
                raise Invalid("Некорректные фигурные скобки в сообщении Mattermost") from exc
            if not node.get("error"):
                raise Invalid(f"Укажите переход при ошибке Mattermost для {node['id']}")
        if kind == "image_worker":
            prompt = node.get("prompt")
            if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
                raise Invalid(f"Заполните инструкцию обработки в шаге {node['id']}")
            try:
                for _, variable, spec, conversion in string.Formatter().parse(prompt):
                    if variable is not None and (
                        variable not in IMAGE_PROMPT_VARIABLES or spec or conversion
                    ):
                        raise Invalid("В инструкции воркера используется неизвестная переменная")
            except ValueError as exc:
                raise Invalid("Некорректные фигурные скобки в инструкции воркера") from exc
            if not node.get("error"):
                raise Invalid(f"Укажите переход при ошибке воркера для {node['id']}")
        if kind == "approval":
            yes, no = approval_variants(node, "accept"), approval_variants(node, "reject")
            if (not yes or not no or yes & no):
                raise Invalid("Укажите разные ответы для подтверждения и отказа")
            if not isinstance(node.get("hours"), (int, float)) or not 0 < node["hours"] <= 72:
                raise Invalid("Укажите срок ожидания ответа от 1 до 72 часов")
        if kind in {"ask_text", "ask_photo", "ask_input", "choice"}:
            if node.get("field") not in FIELDS[product_type]:
                raise Invalid("Поле не принадлежит выбранному типу товара")
            if kind == "ask_input" and node["field"] in {"photos", "template_id"}:
                raise Invalid("Шаг «Текст или фото» должен сохранять текст в текстовое поле")
            if kind != "ask_input" and (kind == "ask_photo") != (node["field"] == "photos"):
                raise Invalid("Для фотографий используйте шаг Запрос фото")
            if node["field"] == "template_id" and kind != "choice":
                raise Invalid("Шаблон выбирается шагом Выбор")
        if kind in {"ask_photo", "ask_input"}:
            low, high = node.get("min"), node.get("max")
            if (
                not isinstance(low, int)
                or not isinstance(high, int)
                or not 1 <= low <= high
            ):
                raise Invalid("Укажите минимум и максимум фото")
            if product_type == "collage" and high > 8:
                raise Invalid("В коллаже максимум 8 фото")
            if kind == "ask_photo" and low < high and not node.get("accept", "").strip():
                raise Invalid("Для набора фото укажите ответ, завершающий загрузку")
            if kind == "ask_input" and not node.get("otherwise"):
                raise Invalid("Для шага «Текст или фото» укажите переход после фотографии")
        if kind == "confirm" and not node.get("accept", "").strip():
            raise Invalid("Укажите точный ответ для подтверждения")
        position = node.get("position")
        if position is not None and (
            not isinstance(position, dict)
            or not isinstance(position.get("x"), (int, float))
            or not isinstance(position.get("y"), (int, float))
        ):
            raise Invalid(f"У шага {node['id']} некорректная позиция на схеме")
        if (
            kind == "choice"
            and node.get("field") != "template_id"
            and not node.get("choices")
        ):
            raise Invalid("Добавьте варианты выбора")
        if kind == "condition":
            if node.get("field") not in FIELDS[product_type]:
                raise Invalid(
                    f"В шаге «{node.get('title') or node['id']}» выберите, какие собранные данные проверять"
                )
            if not node.get("otherwise"):
                raise Invalid(
                    f"В шаге «{node.get('title') or node['id']}» выберите переход «Если условие не выполнено»"
                )
        targets = [node.get("next"), node.get("otherwise"), node.get("error")]
        if kind not in {"end", "ready"} and not node.get("next"):
            raise Invalid(f"Укажите следующий шаг для {node['id']}")
        if any(t and t not in index for t in targets):
            raise Invalid("Переход ссылается на отсутствующий шаг")
        if kind == "handoff" and index[node["next"]]["kind"] != "end":
            raise Invalid(
                f"Шаг «{node.get('title') or node['id']}» передаёт чат менеджеру "
                "и останавливает автоматический сценарий. "
                "Соедините его с шагом «Завершить»; действия до передачи "
                "расположите перед этим шагом"
            )
    visited, stack = set(), set()

    def visit(key):
        if key in stack:
            raise Invalid("Циклы не поддерживаются: используйте ожидание ответа")
        if key in visited:
            return
        stack.add(key)
        node = index[key]
        for target in (node.get("next"), node.get("otherwise"), node.get("error")):
            if target:
                visit(target)
        stack.remove(key)
        visited.add(key)

    visit(starts[0]["id"])
    if visited != set(index):
        raise Invalid("Есть недостижимые шаги")
    checked = set()

    def check_ready_path(key, collected=frozenset(), confirmed=False):
        state = (key, collected, confirmed)
        if state in checked:
            return
        checked.add(state)
        node = index[key]
        if node["kind"] in {"ask_text", "ask_photo", "choice"}:
            collected = collected | {node["field"]}
            confirmed = False
        if node["kind"] == "ask_input":
            check_ready_path(node["next"], collected | {node["field"]}, False)
            check_ready_path(node["otherwise"], collected | {"photos"}, False)
            if node.get("error"):
                check_ready_path(node["error"], collected, confirmed)
            return
        if node["kind"] == "confirm":
            confirmed = True
        if node["kind"] == "condition" and node["field"] not in collected:
            label = FIELD_LABELS.get(node["field"], node["field"])
            raise Invalid(
                f"Шаг «{node.get('title') or node['id']}» проверяет «{label}», "
                "но до него эти данные ещё не собраны. "
                "Перед условием добавьте шаг «Задать вопрос и сохранить ответ» с тем же полем"
            )
        if node["kind"] == "image_worker" and "photos" not in collected:
            raise Invalid(
                f"До шага «{node.get('title') or node['id']}» нужно получить фотографию покупателя"
            )
        if node["kind"] == "ready" and (
            not required <= collected or (requires_confirmation and not confirmed)
        ):
            raise Invalid(
                "Каждый путь к готовности должен собрать обязательные поля"
                + (" и затем запросить подтверждение" if requires_confirmation else "")
            )
        for target in (node.get("next"), node.get("otherwise")):
            if target:
                check_ready_path(target, collected, confirmed)
        if node.get("error"):
            # An invalid answer did not populate its field or confirm the brief.
            check_ready_path(node["error"], state[1], state[2])

    check_ready_path(starts[0]["id"])
    return index


def publish(con, scenario_id, actor):
    scenario = con.execute(
        "SELECT * FROM scenarios WHERE id=?", (scenario_id,)
    ).fetchone()
    graph = json.loads(scenario["draft"])
    validate(graph, scenario["product_type"])
    number = con.execute(
        "SELECT COALESCE(MAX(number),0)+1 FROM versions WHERE scenario_id=?",
        (scenario_id,),
    ).fetchone()[0]
    vid = con.execute(
        "INSERT INTO versions(scenario_id,number,graph,author,created_at) VALUES(?,?,?,?,?)",
        (scenario_id, number, db.dump(graph), actor, db.now()),
    ).lastrowid
    con.execute("UPDATE scenarios SET published=? WHERE id=?", (vid, scenario_id))
    db.audit(con, actor, "scenario.published", "version", vid)
    return vid


def queue_text(con, chat, instance, text, key, actor="bot"):
    if not text.strip():
        raise Invalid("Пустой текст сообщения")
    if len(text) > policy.MESSAGE_MAX_CHARS:
        raise Invalid("Исходящий текст длиннее 1000 символов")
    con.execute(
        "INSERT OR IGNORE INTO outbox(chat_id,instance_id,actor,body,dedup,epoch,created_at) VALUES(?,?,?,?,?,?,?)",
        (chat["id"], instance, actor, text, key, chat["epoch"], db.now()),
    )


def start(con, item_id, chat_id):
    item = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not item or not chat or item["account_id"] != chat["account_id"]:
        raise Invalid("Заказ и чат должны принадлежать одному кабинету")
    if not con.execute(
        "SELECT 1 FROM chat_items WHERE chat_id=? AND item_id=?", (chat_id, item_id)
    ).fetchone():
        raise Invalid("Сначала подтвердите связь чата с позицией")
    mapping = con.execute(
        "SELECT m.*,s.published FROM mappings m JOIN scenarios s ON s.id=m.scenario_id WHERE m.id=?",
        (item["mapping_id"],),
    ).fetchone()
    if not mapping or not mapping["published"]:
        raise Invalid("Нет привязки к опубликованному сценарию")
    graph = json.loads(
        con.execute(
            "SELECT graph FROM versions WHERE id=?", (mapping["published"],)
        ).fetchone()[0]
    )
    first = next(n["id"] for n in graph["nodes"] if n["kind"] == "start")
    con.execute(
        "INSERT INTO instances(item_id,chat_id,version_id,node,template_ids) VALUES(?,?,?,?,?)",
        (item_id, chat_id, mapping["published"], first, mapping["template_ids"]),
    )
    db.audit(con, "system", "scenario.started", "item", item_id, chat_id)


def complete(con, instance, graph, fields):
    for node in graph["nodes"]:
        field = node.get("field")
        if node.get("required") and not fields.get("photos" if node["kind"] == "ask_input" else field):
            return False
        if node["kind"] in {"ask_photo", "ask_input"} and (node.get("required") or fields.get("photos")):
            photos = fields.get("photos", [])
            if not node["min"] <= len(photos) <= node["max"]:
                return False
            for mid in photos:
                if not con.execute(
                    "SELECT 1 FROM media WHERE id=? AND chat_id=? AND item_id=?",
                    (mid, instance["chat_id"], instance["item_id"]),
                ).fetchone():
                    return False
                if not (db.DATA / "media" / mid).is_file():
                    return False
        if field == "template_id" and fields.get(field):
            if str(fields[field]) not in [
                str(x) for x in json.loads(instance["template_ids"])
            ]:
                return False
            if not con.execute(
                "SELECT 1 FROM templates WHERE id=? AND active=1", (fields[field],)
            ).fetchone():
                return False
    return True


def settled(con, instance):
    return (
        not con.execute(
            "SELECT 1 FROM events WHERE chat_id=? AND state='pending'",
            (instance["chat_id"],),
        ).fetchone()
        and not con.execute(
            "SELECT 1 FROM outbox WHERE chat_id=? AND state IN ('pending','unknown','failed')",
            (instance["chat_id"],),
        ).fetchone()
        and not con.execute(
            "SELECT 1 FROM retailcrm_actions WHERE instance_id=? "
            "AND state IN ('pending','running','unknown')",
            (instance["id"],),
        ).fetchone()
        and not con.execute(
            "SELECT 1 FROM image_jobs WHERE instance_id=? "
            "AND state NOT IN ('completed','failed')", (instance["id"],),
        ).fetchone()
    )


def image_prompt(node, item, fields):
    labels = {
        "background": "Фон", "caption": "Надпись", "wishes": "Пожелания",
        "template_id": "Шаблон",
    }
    summary = "\n".join(
        f"{label}: {fields[key]}" for key, label in labels.items() if fields.get(key)
    )
    values = {key: str(fields.get(key) or "") for key in IMAGE_PROMPT_VARIABLES}
    values.update({
        "posting": item["posting"], "offer_id": item["offer_id"],
        "product_name": item["name"], "quantity": item["quantity"],
        "summary": summary,
    })
    try:
        return node["prompt"].format_map(values)
    except (KeyError, ValueError) as exc:
        raise image_tasks.ImageTaskError("image_worker_prompt_invalid") from exc


def advance(con, instance_id, reply=None, *, simulate_external=False):
    instance = con.execute(
        "SELECT * FROM instances WHERE id=?", (instance_id,)
    ).fetchone()
    chat = con.execute(
        "SELECT * FROM chats WHERE id=?", (instance["chat_id"],)
    ).fetchone()
    if (
        chat["mode"] != "bot"
        or chat["active_item"] != instance["item_id"]
        or instance["status"] not in {"collecting", "waiting_integration", "waiting_mockup", "waiting_approval"}
    ):
        return
    item = con.execute(
        "SELECT * FROM items WHERE id=?", (instance["item_id"],)
    ).fetchone()
    version = con.execute(
        "SELECT graph FROM versions WHERE id=?", (instance["version_id"],)
    ).fetchone()
    graph = json.loads(version[0])
    index = {n["id"]: n for n in graph["nodes"]}
    fields = json.loads(instance["fields"])
    node_id, prompted = instance["node"], instance["prompted"]
    revision = instance["revision"] + 1
    confirmed = instance["confirmed_at"]
    status = "collecting"
    for _ in range(len(index) + 1):
        node = index[node_id]
        kind = node["kind"]
        answer_target = None
        if kind in TEXT_KINDS and not prompted:
            labels = {
                "photos": "Фото",
                "background": "Фон",
                "caption": "Надпись",
                "wishes": "Пожелания",
                "template_id": "Шаблон",
            }
            allowed_templates = []
            for tid in json.loads(instance["template_ids"]):
                entry = con.execute(
                    "SELECT id,name FROM templates WHERE id=? AND active=1", (tid,)
                ).fetchone()
                if entry:
                    allowed_templates.append(f"{entry['id']}: {entry['name']}")
            visible_fields = {
                k: (str(len(v)) if k == "photos" else str(v)) for k, v in fields.items()
            }
            summary = "\n".join(f"{labels[k]}: {v}" for k, v in visible_fields.items())
            values = {
                **visible_fields,
                "posting": item["posting"],
                "sku": item["offer_id"],
                "offer_id": item["offer_id"],
                "summary": summary,
                "templates": "\n".join(allowed_templates),
            }
            try:
                text = node["text"].format_map(values)
            except (KeyError, ValueError):
                status = "needs_manager"
                break
            queue_text(
                con, chat, instance_id, text, f"node:{instance_id}:{revision}:{node_id}"
            )
            prompted = 1
            # A message already in flight before the question is not its answer.
            if kind in WAITING:
                if kind == "approval":
                    status = "waiting_approval"
                break
        if kind in WAITING:
            if reply is None:
                if kind == "await_mockup":
                    status = "waiting_mockup"
                elif kind == "approval":
                    status = "waiting_approval"
                break
            text = reply.get("text", "").strip()
            valid = True
            if kind == "ask_text":
                valid = bool(text) and (
                    not node.get("max_length") or len(text) <= node["max_length"]
                )
                if valid:
                    fields[node["field"]] = text
                    confirmed = None
            elif kind == "ask_input":
                incoming = reply.get("media_ids", [])
                if incoming:
                    photos = list(dict.fromkeys(fields.get("photos", []) + incoming))
                    valid = node["min"] <= len(photos) <= node["max"] and all(
                        con.execute(
                            "SELECT 1 FROM media WHERE id=? AND chat_id=? AND item_id=?",
                            (mid, chat["id"], item["id"]),
                        ).fetchone()
                        and (db.DATA / "media" / mid).is_file()
                        for mid in incoming
                    )
                    if valid:
                        fields["photos"] = photos
                        confirmed = None
                        answer_target = node["otherwise"]
                else:
                    valid = bool(text) and (
                        not node.get("max_length") or len(text) <= node["max_length"]
                    )
                    if valid:
                        fields[node["field"]] = text
                        confirmed = None
            elif kind == "ask_photo":
                incoming = reply.get("media_ids", [])
                existing = fields.get(node["field"], [])
                photos = list(dict.fromkeys(existing + incoming))
                finished = (
                    bool(node.get("accept"))
                    and text.casefold() == node["accept"].casefold()
                )
                valid = (bool(incoming) or finished) and len(photos) <= node["max"]
                valid = valid and all(
                    con.execute(
                        "SELECT 1 FROM media WHERE id=? AND chat_id=? AND item_id=?",
                        (mid, chat["id"], item["id"]),
                    ).fetchone()
                    and (db.DATA / "media" / mid).is_file()
                    for mid in incoming
                )
                if valid:
                    fields[node["field"]] = photos
                    confirmed = None
                    if finished and len(photos) < node["min"]:
                        valid = False
                    elif len(photos) < node["max"] and not finished:
                        break
            elif kind == "choice":
                allowed = (
                    [str(x) for x in json.loads(instance["template_ids"])]
                    if node["field"] == "template_id"
                    else node["choices"]
                )
                valid = text in allowed
                if node["field"] == "template_id":
                    valid = valid and bool(
                        con.execute(
                            "SELECT 1 FROM templates WHERE id=? AND active=1", (text,)
                        ).fetchone()
                    )
                if valid:
                    fields[node["field"]] = text
                    confirmed = None
            elif kind == "confirm":
                valid = text.casefold() == node[
                    "accept"
                ].strip().casefold() and complete(con, instance, graph, fields)
                if valid:
                    confirmed = db.now()
            elif kind == "await_mockup":
                valid = reply.get("manager_file_sent") is True
            elif kind == "approval":
                if reply.get("approval_timeout") is True:
                    fields["approval_outcome"] = f"Нет ответа за {node['hours']:g} ч."
                    valid = True
                elif approval_phrase(text) in approval_variants(node, "accept"):
                    fields["approval_outcome"] = "Макет подтверждён покупателем"
                    valid = True
                elif approval_phrase(text) in approval_variants(node, "reject"):
                    fields["approval_outcome"] = "Покупатель отказался от макета"
                    valid = True
                else:
                    valid = False
                if valid:
                    con.execute("UPDATE instances SET approval_due_at=NULL WHERE id=?", (instance_id,))
            if not valid:
                fields["integration_error"] = "Ответ покупателя не соответствует ожидаемому формату"
                db.audit(
                    con,
                    "system",
                    "answer.rejected",
                    "instance",
                    instance_id,
                    chat["id"],
                )
                if node.get("error"):
                    node_id = node["error"]
                    prompted = 0
                    reply = None
                    continue
                status = "needs_manager"
                break
            db.audit(
                con,
                "system",
                "answer.accepted",
                "instance",
                instance_id,
                chat["id"],
            )
            reply = None
        if kind == "mattermost":
            if simulate_external:
                node_id, prompted = node["next"], 0
                continue
            action = con.execute(
                "SELECT * FROM mattermost_actions WHERE instance_id=? AND node_id=?",
                (instance_id, node_id),
            ).fetchone()
            if action is None:
                action_id = con.execute(
                    "INSERT INTO mattermost_actions(instance_id,node_id,created_at) VALUES(?,?,?)",
                    (instance_id, node_id, db.now()),
                ).lastrowid
                con.execute(
                    "INSERT INTO jobs(kind,account_id,payload,created_at) VALUES('mattermost_send',?,?,?)",
                    (item["account_id"], db.dump({"action_id": action_id}), db.now()),
                )
                db.audit(con, "system", "mattermost.queued", "mattermost_action", action_id, chat["id"])
                status = "waiting_integration"
                break
            if action["state"] == "sent":
                node_id, prompted = node["next"], 0
                continue
            if action["state"] == "failed":
                node_id, prompted = node["error"], 0
                continue
            status = "needs_manager" if action["state"] == "unknown" else "waiting_integration"
            break
        if kind == "image_worker":
            action = con.execute(
                "SELECT * FROM image_jobs WHERE instance_id=? AND node_id=?",
                (instance_id, node_id),
            ).fetchone()
            if action is None:
                photos = fields.get("photos") or []
                if len(photos) != 1:
                    fields["integration_error"] = (
                        "В брифе нет исходного фото" if not photos
                        else image_tasks.ERROR_LABELS["image_worker_photo_ambiguous"]
                    )
                    if photos:
                        status = "needs_manager"
                        break
                    node_id, prompted = node["error"], 0
                    continue
                try:
                    width, height = image_tasks.dimensions_from_article(item["offer_id"])
                    prompt = image_prompt(node, item, fields)
                    if simulate_external:
                        if not 1 <= len(prompt.strip()) <= 4000:
                            raise image_tasks.ImageTaskError("image_worker_prompt_invalid")
                        con.execute(
                            "INSERT INTO image_jobs(item_id,media_id,media_ids,prompt,width_cm,height_cm,state,"
                            "print_file,created_at,updated_at,instance_id,node_id) "
                            "VALUES(?,?,?,?,?,?,'completed','simulation',?,?,?,?)",
                            (item["id"], photos[0], db.dump(photos), prompt, width, height, db.now(), db.now(),
                             instance_id, node_id),
                        )
                    else:
                        image_tasks.enqueue(
                            con, item["id"], photos, prompt, width, height, "system",
                            instance_id=instance_id, node_id=node_id,
                        )
                except image_tasks.ImageTaskError as exc:
                    fields["integration_error"] = image_tasks.ERROR_LABELS.get(
                        exc.code, "Не удалось поставить макет в очередь. Проверьте подключение и данные заказа."
                    )
                    node_id, prompted = node["error"], 0
                    continue
                db.audit(con, "system", "image_job.scenario_queued", "instance", instance_id, chat["id"])
                if simulate_external:
                    # The in-memory test job stands in for a worker result.
                    node_id, prompted = node["next"], 0
                    continue
                status = "waiting_integration"
                break
            if action["state"] == "completed":
                node_id, prompted = node["next"], 0
                continue
            if action["state"] == "failed":
                fields["integration_error"] = image_tasks.ERROR_LABELS.get(
                    action["error"], "Обработка макета завершилась ошибкой. Проверьте задание в очереди."
                )
                node_id, prompted = node["error"], 0
                continue
            status = "waiting_integration"
            break
        if kind in {"retailcrm", "retailcrm_note"}:
            if simulate_external:
                db.audit(
                    con,
                    "simulation",
                    "retailcrm.simulated",
                    "instance",
                    instance_id,
                    chat["id"],
                )
                node_id = node["next"]
                prompted = 0
                continue
            action = con.execute(
                "SELECT * FROM retailcrm_actions WHERE instance_id=? AND node_id=?",
                (instance_id, node_id),
            ).fetchone()
            if action is None:
                action_id = con.execute(
                    "INSERT INTO retailcrm_actions(instance_id,node_id,external_id,created_at,kind) "
                    "VALUES(?,?,?,?,?)",
                    (instance_id, node_id, retailcrm.external_id(instance_id), db.now(),
                     "note" if kind == "retailcrm_note" else "create"),
                ).lastrowid
                con.execute(
                    "INSERT INTO jobs(kind,account_id,payload,created_at) "
                    "VALUES(?,(SELECT account_id FROM items WHERE id=?),?,?)",
                    ("retailcrm_note" if kind == "retailcrm_note" else "retailcrm_create",
                     instance["item_id"], db.dump({"action_id": action_id}), db.now()),
                )
                db.audit(
                    con,
                    "system",
                    "retailcrm.queued",
                    "retailcrm_action",
                    action_id,
                    chat["id"],
                )
                status = "waiting_integration"
                break
            if action["state"] == "sent":
                node_id = node["next"]
                prompted = 0
                continue
            if action["state"] == "failed":
                if node.get("error"):
                    node_id = node["error"]
                    prompted = 0
                    continue
                status = "needs_manager"
                break
            status = (
                "needs_manager"
                if action["state"] == "unknown"
                else "waiting_integration"
            )
            break
        if kind == "handoff":
            status = "needs_manager"
            break
        if kind == "ready":
            confirmation_required = any(
                candidate.get("kind") == "confirm" for candidate in graph["nodes"]
            )
            status = (
                "needs_review"
                if (confirmed or not confirmation_required)
                and complete(con, instance, graph, fields)
                else "needs_manager"
            )
            break
        if kind == "end":
            status = "closed"
            break
        node_id = answer_target or (
            node.get("otherwise")
            if kind == "condition"
            and str(fields.get(node["field"], "")) != node.get("equals", "")
            else node["next"]
        )
        prompted = 0
    if status == "needs_manager":
        takeover(con, chat["id"], "system")
    con.execute(
        "UPDATE instances SET node=?,fields=?,status=?,prompted=?,revision=?,confirmed_at=? WHERE id=?",
        (node_id, db.dump(fields), status, prompted, revision, confirmed, instance_id),
    )
    db.audit(con, "system", f"scenario.{status}", "instance", instance_id, chat["id"])


def takeover(con, chat_id, actor):
    con.execute("UPDATE chats SET mode='manual',epoch=epoch+1 WHERE id=?", (chat_id,))
    con.execute(
        "UPDATE outbox SET state='cancelled' WHERE chat_id=? AND actor='bot' AND state='pending'",
        (chat_id,),
    )
    db.audit(con, actor, "chat.takeover", "chat", chat_id, chat_id)


def resume(con, chat_id, node_id, actor):
    if con.execute(
        "SELECT 1 FROM outbox WHERE chat_id=? AND state IN ('unknown','pending')",
        (chat_id,),
    ).fetchone():
        raise Invalid("Сначала завершите или сверьте исходящие сообщения")
    chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    instance = con.execute(
        "SELECT * FROM instances WHERE chat_id=? AND item_id=?",
        (chat_id, chat["active_item"]),
    ).fetchone()
    if not instance:
        raise Invalid("Сначала выберите позицию и запустите сценарий")
    item = con.execute(
        "SELECT external_status FROM items WHERE id=?", (instance["item_id"],)
    ).fetchone()
    if item and item["external_status"] == "cancelled":
        raise Invalid("Заказ отменён в Ozon. Сценарий нельзя возобновить")
    graph = json.loads(
        con.execute(
            "SELECT graph FROM versions WHERE id=?", (instance["version_id"],)
        ).fetchone()[0]
    )
    if node_id not in {n["id"] for n in graph["nodes"]}:
        raise Invalid("Выберите существующий шаг зафиксированной версии")
    con.execute("UPDATE chats SET mode='bot',epoch=epoch+1 WHERE id=?", (chat_id,))
    con.execute(
        "UPDATE instances SET node=?,status='collecting',prompted=0,confirmed_at=NULL,reviewed_at=NULL,ready_at=NULL WHERE id=?",
        (node_id, instance["id"]),
    )
    db.audit(con, actor, "chat.resumed", "instance", instance["id"], chat_id)
    advance(con, instance["id"])


def approve(con, instance_id, actor):
    instance = con.execute(
        "SELECT * FROM instances WHERE id=?", (instance_id,)
    ).fetchone()
    graph = json.loads(
        con.execute(
            "SELECT graph FROM versions WHERE id=?", (instance["version_id"],)
        ).fetchone()[0]
    )
    settings = db.config(con)
    hours = settings.get("handoff_hours")
    if hours is None:
        raise Invalid("Администратор должен настроить N часов в кабинете Folio")
    confirmation_required = any(
        node.get("kind") == "confirm" for node in graph["nodes"]
    )
    if (
        instance["status"] != "needs_review"
        or (confirmation_required and not instance["confirmed_at"])
        or not complete(con, instance, graph, json.loads(instance["fields"]))
        or not settled(con, instance)
    ):
        raise Invalid("Бриф не прошёл проверку комплектности")
    origin = (
        instance["confirmed_at"]
        if settings.get("timer_origin") == "confirmation" and instance["confirmed_at"]
        else db.now()
    )
    eligible = (datetime.fromisoformat(origin) + timedelta(hours=hours)).isoformat()
    con.execute(
        "UPDATE instances SET status='waiting_release',reviewed_at=?,reviewed_by=?,ready_at=? WHERE id=?",
        (db.now(), actor, eligible, instance_id),
    )
    db.audit(con, actor, "brief.reviewed", "instance", instance_id, instance["chat_id"])


def release_due(con):
    for instance in con.execute(
        "SELECT * FROM instances WHERE status='waiting_release' AND ready_at<=?",
        (db.now(),),
    ).fetchall():
        graph = json.loads(
            con.execute(
                "SELECT graph FROM versions WHERE id=?", (instance["version_id"],)
            ).fetchone()[0]
        )
        if not settled(con, instance):
            continue
        confirmation_required = any(
            node.get("kind") == "confirm" for node in graph["nodes"]
        )
        valid = (
            (instance["confirmed_at"] or not confirmation_required)
            and instance["reviewed_at"]
            and complete(con, instance, graph, json.loads(instance["fields"]))
        )
        state = "ready" if valid else "needs_manager"
        con.execute("UPDATE instances SET status=? WHERE id=?", (state, instance["id"]))
        db.audit(
            con,
            "system",
            f"brief.{state}",
            "instance",
            instance["id"],
            instance["chat_id"],
        )


def outgoing_confirmed(con, outbox_id):
    row = con.execute("SELECT * FROM outbox WHERE id=? AND state='sent'", (outbox_id,)).fetchone()
    if not row or not row["instance_id"]:
        return
    instance = con.execute("SELECT * FROM instances WHERE id=?", (row["instance_id"],)).fetchone()
    if not instance:
        return
    graph = json.loads(con.execute("SELECT graph FROM versions WHERE id=?", (instance["version_id"],)).fetchone()[0])
    node = next((item for item in graph["nodes"] if item["id"] == instance["node"]), None)
    if row["kind"] == "file" and node and node["kind"] == "await_mockup" and instance["status"] == "waiting_mockup":
        advance(con, instance["id"], {"manager_file_sent": True})
    elif (row["kind"] == "text" and node and node["kind"] == "approval"
          and row["dedup"].endswith(f":{node['id']}")
          and instance["status"] == "waiting_approval"):
        deadline = (datetime.fromisoformat(db.now()) + timedelta(hours=node["hours"])).isoformat()
        con.execute("UPDATE instances SET approval_due_at=? WHERE id=? AND approval_due_at IS NULL",
                    (deadline, instance["id"]))
        db.audit(con, "system", "approval.timer_started", "instance", instance["id"], instance["chat_id"])


def expire_approvals(con):
    for instance in con.execute(
        "SELECT i.* FROM instances i JOIN chats c ON c.id=i.chat_id "
        "JOIN items item ON item.id=i.item_id "
        "WHERE i.status='waiting_approval' AND i.approval_due_at IS NOT NULL "
        "AND i.approval_due_at<=? AND c.mode='bot' AND item.external_status!='cancelled' "
        "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.chat_id=i.chat_id AND e.state='pending')",
        (db.now(),)
    ).fetchall():
        advance(con, instance["id"], {"approval_timeout": True})
