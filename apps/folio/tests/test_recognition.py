"""Buyer-language regression corpus; no API calls or customer messages."""
import unittest
import json

from app.wish_intent import classify
from app.scenarios import approval_decision
from app.simulation import simulate


WISHES = (
    'Сделать более четкое изображение', 'Сделайте снимок чётче',
    'Повысьте резкость', 'Улучшите качество', 'Повысить качество',
    'Замените фон', 'Поменяйте фон', 'Можно поменять фон?',
    'Можно сделать фото светлее?', 'Хочу фон как на фото',
    'Не меняйте фон, улучшите фото', 'Фон не менять',
    'Оставьте как есть', 'Оставить как есть', 'Всё как есть',
    'Без пожеланий', 'Пожеланий нет', 'Нет пожеланий', 'Без изменений',
    'Ничего не менять', 'Ничего менять не надо', 'Ничего менять не нужно',
    'Без согласования', 'На ваше усмотрение',
    'Почётче', 'Поярче', 'Посветлее', 'Потемнее', 'Чётче',
    'Более четкое изображение', 'Улучшение качества', 'Замена фона',
    'Белый фон', 'Фон голубой', 'Светлый', 'Тёмный фон',
    'Фон море', 'Фон лес', 'Фон природа', 'Фон как на фото',
    'В стиле акварели', 'В стиле dream art',
    'Хочу в стиле акварели dream art', 'Добавьте надпись',
    'Уберите человека на заднем плане', 'Сохраните лицо и одежду',
    'Осветлите лицо', 'Уменьшите яркость', 'Уберите размытость',
    'Сделайте фон посветлее', 'Добавьте цветы',
    'Сделайте акцент на лице', 'Фон оставьте, а фото сделайте ярче',
    'Сделайте фон, например море',
    'Нет, фон менять не нужно, только улучшите качество фотографии',
)
OPTIONS = (
    'Какие есть варианты фона?', 'Покажите примеры оформления',
    'Есть каталог фонов?', 'Можно образцы фона?',
    'Какие варианты для оформления фото?', 'Фон: какие есть варианты?',
)
MANAGER = (
    '', ' ', 'Здравствуйте', 'Добрый день', 'Спасибо', 'Да', 'Нет',
    '👍', '???', 'Не понимаю как это работает', 'Позовите менеджера',
    'Когда доставка?', 'Где заказ?', 'Какая цена?', 'Как оплатить?',
    'Хочу отменить заказ', 'Хочу поменять адрес доставки',
    'Нужен возврат', 'Можно скидку?', 'Замените фон, когда доставка?',
    'Какие варианты фона и сколько стоит доставка?',
    'Улучшите качество, позовите оператора',
    'Какая стоимость замены фона?', 'Сделайте быстрее',
    'Хочу поменять фон, сколько стоит?',
    'Сдлайти чтота', 'https://example.com', '49778856-0150-1',
)


class RecognitionTest(unittest.TestCase):
    def test_wishes_in_both_message_orders(self):
        graph = {'nodes': [
            {'id': 'start', 'kind': 'start', 'next': 'input'},
            {'id': 'input', 'kind': 'ask_input', 'field': 'background',
             'text': 'Фото или пожелания?', 'min': 1, 'max': 1,
             'wish_guard': True, 'options': 'manager', 'error': 'manager',
             'next': 'photo', 'otherwise': 'wish'},
            {'id': 'photo', 'kind': 'ask_photo', 'field': 'photos',
             'text': 'Пришлите фото', 'min': 1, 'max': 1, 'next': 'approval'},
            {'id': 'wish', 'kind': 'ask_input', 'field': 'background',
             'text': 'Пожелания?', 'min': 1, 'max': 1,
             'wish_guard': True, 'options': 'manager', 'error': 'manager',
             'next': 'approval', 'otherwise': 'manager'},
            {'id': 'approval', 'kind': 'ask_text', 'field': 'wishes',
             'text': 'Нужно согласование?', 'next': 'ready'},
            {'id': 'ready', 'kind': 'ready'},
            {'id': 'manager', 'kind': 'handoff', 'next': 'end'},
            {'id': 'end', 'kind': 'end'},
        ]}
        for text in WISHES:
            for replies in ([text, 'photo:1'], ['photo:1', text]):
                with self.subTest(replies=replies):
                    result = simulate(graph, 'portrait_background', replies)
                    fields = json.loads(result['instance']['fields'])
                    self.assertEqual(fields.get('background'), text)
                    self.assertEqual(len(fields.get('photos', [])), 1)
                    self.assertNotIn('integration_error', fields)
                    self.assertEqual(result['timeline'][-1]['body'], 'Нужно согласование?')

    def test_wishes_with_case_whitespace_and_politeness(self):
        for text in WISHES:
            for value in (text, text.upper(), '  ' + text + '  ',
                          'Здравствуйте! ' + text + ', пожалуйста. Спасибо!',
                          text.replace(' ', '\n'), text.replace('ё', 'е')):
                with self.subTest(text=value):
                    self.assertEqual(classify(value), 'wish')

    def test_options(self):
        for text in OPTIONS:
            with self.subTest(text=text):
                self.assertEqual(classify(text), 'options')

    def test_unrelated_and_mixed_questions_need_manager(self):
        for text in MANAGER:
            with self.subTest(text=text):
                self.assertEqual(classify(text), 'manager')

    def test_approval_dictionary(self):
        node = {'dictionary_version': 2}
        groups = {
            'accept': ('Да', 'Да, согласовано', 'Подтверждаю макет',
                       'Всё устраивает', 'Можно печатать', 'Да, спасибо!',
                       'Макет согласован', 'Согласна', 'ОК'),
            'reject': ('Нет', 'Не согласна', 'Макет не нравится',
                       'Нет, зачем убрали фон, я хочу чтобы фон был',
                       'Переделайте макет', 'Не подходит, сделайте светлее',
                       'Нет, зачем убрали фон? Я хочу чтобы фон был.'),
            None: ('', '👍', 'Спасибо', 'Да, но поменяйте фон',
                   'Можно сделать ярче?', 'Не знаю', 'Наверное да',
                   'Да, нет', 'Доставка завтра?', 'Соглсовано',
                   'Да?', 'Нет замечаний', 'Нет, согласовано'),
        }
        for expected, texts in groups.items():
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(approval_decision(node, text), expected)
