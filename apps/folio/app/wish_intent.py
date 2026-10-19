"""Conservative, versioned intake routing. Unknown language is NOT a wish."""
import re


def classify(text):
    value = re.sub(r"\s+", " ", text.casefold().replace("ё", "е")).strip()
    if re.search(r"(вариант|пример|образец|каталог).*(фон|оформлен|фото|стил)|"
                 r"(фон|оформлен|стил).*(вариант|пример|образец|каталог)", value):
        return "options"
    if re.search(r"достав|оплат|возврат|отмен|стоимост|цен[ауы]|скидк|когда|где заказ|менеджер|оператор", value):
        return "manager"
    if re.fullmatch(r"(?:здравствуйте[,.! ]*)?(?:без согласования|без изменений|оставьте как есть|оставить как есть)[.! ]*", value):
        return "wish"
    # Intake asks about processing as well as backgrounds. Quality/sharpness
    # requests must not fall through to handoff just because they omit "фото".
    if re.search(r"(замен|измен|убер|убра|добав|сдела|остав|улучш|осветл|затемн|сохран|повыс|увелич|усил|хочу|нужен|нужна|нужно|пожелани)", value) and re.search(
            r"фон|задн|цвет|свет|ярк|темн|светл|лиц|волос|одежд|комнат|интерьер|портрет|рисунк|акварел|dream\s*art|коллаж|надпис|фото|изображен|снимок|снимк|четк|резкост|детализац|размытост|море|лес|цветы", value):
        return "wish"
    if re.search(r"\b(?:улучш\w*|повыс\w*)\s+качество\b", value):
        return "wish"
    if re.fullmatch(r"(?:фон\s+)?(?:белый|черный|голубой|синий|зеленый|розовый|бежевый|серый)(?:\s+фон)?[.! ]*", value):
        return "wish"
    return "manager"
