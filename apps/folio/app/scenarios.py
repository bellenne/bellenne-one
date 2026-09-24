"""A deliberately finite, validated graph interpreter; graphs and text live in DB."""

import json
import string
from datetime import datetime, timedelta

from . import db, policy, retailcrm

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
    "choice": "Выбор",
    "condition": "Условие",
    "wait": "Ожидание ответа",
    "handoff": "Менеджеру",
    "confirm": "Подтверждение покупателем",
    "retailcrm": "Создать сделку в RetailCRM",
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
WAITING = {"ask_text", "ask_photo", "choice", "wait", "confirm"}
TEXT_KINDS = WAITING | {"send"}
RETAILCRM_VARIABLES = {
    "posting",
    "sku",
    "offer_id",
    "product_name",
    "quantity",
    "summary",
}


class Invalid(ValueError):
    pass


def normalize(graph, product_type):
    """Apply product invariants that are not administrator-facing settings."""
    changed = False
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    photo_nodes = [
        node
        for node in nodes
        if node.get("kind") == "ask_photo" and node.get("field") == "photos"
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
        if n.get("kind") in {"ask_text", "ask_photo", "choice"}
    }
    required = {n.get("field") for n in nodes if n.get("required")}
    if "photos" not in fields:
        raise Invalid("Добавьте шаг «Запросить фото»: без исходного фото бриф нельзя проверить")
    if "photos" not in required:
        raise Invalid("Сделайте шаг «Запросить фото» обязательной частью брифа")
    if product_type == "template_art" and "template_id" not in fields:
        raise Invalid("Добавьте шаг «Предложить выбор шаблона»")
    if product_type == "template_art" and "template_id" not in required:
        raise Invalid("Выбор шаблона должен быть обязательной частью брифа")
    if not any(n.get("kind") == "ready" for n in nodes):
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
        if kind == "retailcrm":
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
        if kind in {"ask_text", "ask_photo", "choice"}:
            if node.get("field") not in FIELDS[product_type]:
                raise Invalid("Поле не принадлежит выбранному типу товара")
            if (kind == "ask_photo") != (node["field"] == "photos"):
                raise Invalid("Для фотографий используйте шаг Запрос фото")
            if node["field"] == "template_id" and kind != "choice":
                raise Invalid("Шаблон выбирается шагом Выбор")
        if kind == "ask_photo":
            low, high = node.get("min"), node.get("max")
            if (
                not isinstance(low, int)
                or not isinstance(high, int)
                or not 1 <= low <= high
            ):
                raise Invalid("Укажите минимум и максимум фото")
            if product_type == "collage" and high > 8:
                raise Invalid("В коллаже максимум 8 фото")
            if low < high and not node.get("accept", "").strip():
                raise Invalid("Для набора фото укажите ответ, завершающий загрузку")
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
        if node["kind"] == "confirm":
            confirmed = True
        if node["kind"] == "condition" and node["field"] not in collected:
            label = FIELD_LABELS.get(node["field"], node["field"])
            raise Invalid(
                f"Шаг «{node.get('title') or node['id']}» проверяет «{label}», "
                "но до него эти данные ещё не собраны. "
                "Перед условием добавьте шаг «Задать вопрос и сохранить ответ» с тем же полем"
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
        if node.get("required") and not fields.get(field):
            return False
        if node["kind"] == "ask_photo" and (node.get("required") or fields.get(field)):
            photos = fields.get(field, [])
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
    )


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
        or instance["status"] not in {"collecting", "waiting_integration"}
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
                break
        if kind in WAITING:
            if reply is None:
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
            if not valid:
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
        if kind == "retailcrm":
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
                external_id, _ = retailcrm.order_payload(con, instance_id, node)
                action_id = con.execute(
                    "INSERT INTO retailcrm_actions(instance_id,node_id,external_id,created_at) "
                    "VALUES(?,?,?,?)",
                    (instance_id, node_id, external_id, db.now()),
                ).lastrowid
                con.execute(
                    "INSERT INTO jobs(kind,account_id,payload,created_at) "
                    "VALUES('retailcrm_create',(SELECT account_id FROM items WHERE id=?),?,?)",
                    (instance["item_id"], db.dump({"action_id": action_id}), db.now()),
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
        node_id = (
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
