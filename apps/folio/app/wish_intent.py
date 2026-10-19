"""Conservative, versioned intake routing. Unknown language is NOT a wish."""
import re


def classify(text):
    value = re.sub(r"\s+", " ", text.casefold().replace("ё", "е")).strip()
    # Match words, not fragments such as "цена" inside a visual description.
    if re.search(r"\b(?:достав\w*|оплат\w*|возврат\w*|отмен\w*|стоимост\w*|цен[ауы]|скидк\w*|когда|менеджер\w*|оператор\w*)\b|где заказ|сколько стоит", value):
        return "manager"
    if re.search(r"\b(вариант|пример|образец|образц|каталог).*(фон|оформлен|фото|стил)|"
                 r"(фон|оформлен|стил).*\b(вариант|пример|образец|образц|каталог)", value):
        return "options"
    plain = re.sub(r"[^\w\s]", " ", value)
    plain = re.sub(r"\s+", " ", plain).strip()
    plain = re.sub(r"^(?:здравствуйте|добрый день|добрый вечер|доброе утро|привет)\s*", "", plain)
    plain = re.sub(r"\b(?:пожалуйста|спасибо)\b", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    if re.fullmatch(r"без согласования|без изменений|без пожеланий|пожеланий нет|нет пожеланий|"
                    r"оставьте как есть|оставить как есть|все как есть|ничего не менять|"
                    r"ничего менять не надо|ничего менять не нужно|на ваше усмотрение", plain):
        return "wish"
    # Intake asks about processing as well as backgrounds. Quality/sharpness
    # requests must not fall through to handoff just because they omit "фото".
    if re.search(r"\b(?:замен|помен|измен|убер|убра|добав|сдела|остав|улучш|осветл|затемн|сохран|повыс|увелич|усил|уменьш|подправ|поправ|отретуш|хочу|нужен|нужна|нужно|пожелани)\w*", value) and re.search(
            r"фон|задн|цвет|свет|ярк|темн|светл|лиц|волос|одежд|комнат|интерьер|портрет|рисунк|акварел|dream\s*art|коллаж|надпис|фото|изображен|снимок|снимк|четк|резкост|детализац|размытост|море|лес|цветы", value):
        return "wish"
    if re.search(r"\b(?:улучш\w*|повыс\w*)\s+качество\b", value):
        return "wish"
    if re.fullmatch(r"(?:фон\s+)?(?:белый|черный|голубой|синий|зеленый|розовый|бежевый|серый|светлый|темный)(?:\s+фон)?", plain):
        return "wish"
    if re.fullmatch(r"(?:повыше качество|получше качество|почетче|поярче|посветлее|потемнее|"
                    r"четче|ярче|светлее|темнее|более четко|более четкое изображение|"
                    r"фон (?:как на фото|не менять|оставить|море|лес|природа)|"
                    r"(?:замена|улучшение|повышение) (?:фона|качества|резкости)|"
                    r"в стиле (?:акварели|акварель|dream art))", plain):
        return "wish"
    return "manager"
