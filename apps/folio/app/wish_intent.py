"""Conservative, versioned intake routing. Unknown language is NOT a wish."""
import re


_WHAT = r"(?:все|фон|фото|фотографию|изображение)"
_AS_IS = r"(?:как есть|так как есть|так|без изменений)"
_CHANGE = r"(?:менять|изменять|трогать)"
_NEED = r"(?:надо|нужно|стоит)"
# Whole answers only: "оставьте все как есть, но замените фон" must not match.
NO_CHANGES = re.compile(
    rf"(?:{_WHAT} )?(?:оставьте|оставить|оставим|оставляем|оставляйте)(?: {_WHAT})? {_AS_IS}|"
    rf"пусть (?:{_WHAT} )?(?:останется|остается)(?: {_WHAT})? {_AS_IS}|"
    r"(?:все )?как есть|без изменений|без пожеланий|без обработки|без согласования|пожеланий нет|нет пожеланий|"
    rf"(?:фон )?не менять|не менять фон|фон как на фото|фон оставить|оставить фон|оставьте фон|"
    rf"ничего не (?:менять|меняйте|меняем|изменять|изменяйте|трогайте|трогать)|"
    rf"(?:ничего {_CHANGE}|{_CHANGE} ничего) не {_NEED}|не {_NEED} ничего {_CHANGE}"
)


def _plain(value):
    value = re.sub(r"[^\w\s]", " ", value.casefold().replace("ё", "е"))
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^(?:(?:здравствуйте|добрый день|добрый вечер|доброе утро|привет)\s*)+", "", value)
    value = re.sub(r"\b(?:пожалуйста|спасибо)\b", "", value)
    return re.sub(r"\s+", " ", value).strip()


def asks_no_changes(text):
    """True only for a known refusal; any other wording may hide a request."""
    plain = _plain(str(text or ""))
    # A bare "нет" or "оставить" is enough here, but not for routing a chat below.
    return bool(NO_CHANGES.fullmatch(plain)) or plain in {"нет", "оставить"}


def classify(text):
    value = re.sub(r"\s+", " ", text.casefold().replace("ё", "е")).strip()
    # Match words, not fragments such as "цена" inside a visual description.
    if re.search(r"\b(?:достав\w*|оплат\w*|возврат\w*|отмен\w*|стоимост\w*|цен[ауы]|скидк\w*|когда|менеджер\w*|оператор\w*)\b|где заказ|сколько стоит", value):
        return "manager"
    if re.search(r"\b(вариант|пример|образец|образц|каталог).*(фон|оформлен|фото|стил)|"
                 r"(фон|оформлен|стил).*\b(вариант|пример|образец|образц|каталог)", value):
        return "options"
    plain = _plain(value)
    if NO_CHANGES.fullmatch(plain) or plain == "на ваше усмотрение":
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
