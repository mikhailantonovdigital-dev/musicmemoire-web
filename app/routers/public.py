import math
import re
import unicodedata
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import engine, get_db
from app.core.templates import templates
from app.core.storage import StorageError, save_support_file
from app.models import BlogArticle, BlogCategory, Order, OrderEvent, SupportMessage, SupportThread, User
from app.services.background_jobs import get_redis_connection
from app.services.telegram_report_service import notify_new_support_thread, telegram_reporting_enabled

router = APIRouter()

SCREEN_PAGES = {
    "home": {
        "page_title": "Magic Music - персональные песни в подарок",
        "meta_description": "Magic Music - персональные песни в подарок. Расскажите историю текстом или голосом, получите 2 варианта текста бесплатно и выберите лучший вариант для готовой песни.",
        "eyebrow": "Ваша индивидуальная история с Вами навсегда!",
        "title": "Создайте песню по Вашей уникальной истории",
        "lead": (
            "Расскажите историю, получите 2 версии текста бесплатно, выберите лучший вариант и получите песню."
        ),
        "points": [
            "2 версии текста бесплатно",
            "История голосом или текстом",
        ],
        "primary_label": "Хочу песню",
        "primary_href": "/questionnaire/",
        "secondary_label": None,
        "secondary_href": None,
        "overlay_title": "Будем Вам полезны:",
        "overlay_items": [
            "Сначала получаете текст песни, и только потом оплата",
            "Можете рассказать свою историю голосовым",
            "50% скидка на второй и следующие заказы в течение дня",
        ],
        "note": "",
    },
    "portfolio": {
        "page_title": "Портфолио - Magic Music",
        "meta_description": "Портфолио Magic Music: примеры готовых песен на свадьбу, юбилей, годовщину и личные подарки. Слушайте треки прямо на странице.",
        "eyebrow": "Портфолио",
        "title": "Послушайте примеры песен",
        "lead": (
            "Здесь собраны уже созданные песни в нашем сервисе: про любовь, свадьбу, юбилей, семью и личные подарки."
            "Все треки можно прослушать прямо на странице."
        ),
        "points": [
            "Любовь и отношения",
            "Свадьба и юбилей",
            "Семья и личные подарки",
        ],
        "primary_label": "Создать свою песню",
        "primary_href": "/questionnaire/",
        "secondary_label": None,
        "secondary_href": None,
        "overlay_title": "Аудио-примеры",
        "overlay_items": [
            "реальные примеры под разные поводы",
            "прослушивание прямо на странице",
            "быстрый переход из портфолио в анкету",
        ],
        "note": "Тысячи довольных клиентов еженедельно.",
        "tracks": [
            {
                "tag": "Личная история",
                "title": "Песня по личной истории",
                "format": "MP3",
                "mime": "audio/mpeg",
                "url": "https://inrestart.com/portfoliomusic/NDViYjYwYmMtOGE2Zi00MjgzLTllMGYtY2Y5Zjc3Y2ZjOWY3.mp3",
            },
            {
                "tag": "История любви",
                "title": "Если вдруг тебя не станет рядом",
                "format": "MP3",
                "mime": "audio/mpeg",
                "url": "https://inrestart.com/portfoliomusic/[%D0%98%D1%81%D1%82%D0%BE%D1%80%D0%B8%D1%8F%20%D0%BB%D1%8E%D0%B1%D0%B2%D0%B8]%20%D0%95%D1%81%D0%BB%D0%B8%20%D0%B2%D0%B4%D1%80%D1%83%D0%B3%20%D1%82%D0%B5%D0%B1%D1%8F%20%D0%BD%D0%B5%20%D1%81%D1%82%D0%B0%D0%BD%D0%B5%D1%82%20%D1%80%D1%8F%D0%B4%D0%BE%D0%BC.mp3",
            },
            {
                "tag": "Свадьба",
                "title": "Песня на свадьбу Юрия и Натальи",
                "format": "WAV",
                "mime": "audio/wav",
                "url": "https://inrestart.com/portfoliomusic/%D0%9F%D0%B5%D1%81%D0%BD%D1%8F%20%D0%BD%D0%B0%20%D1%81%D0%B2%D0%B0%D0%B4%D1%8C%D0%B1%D1%83%20%D0%AE%D1%80%D0%B8%D1%8F%20%D0%B8%20%D0%9D%D0%B0%D1%82%D0%B0%D0%BB%D1%8C%D1%8F.wav",
            },
            {
                "tag": "Подарок",
                "title": "Подарок Лёше от Катюши",
                "format": "WAV",
                "mime": "audio/wav",
                "url": "https://inrestart.com/portfoliomusic/%D0%9F%D0%BE%D0%B4%D0%B0%D1%80%D0%BE%D0%BA%20%D0%9B%D0%B5%CC%88%D1%88%D0%B5%20%D0%BE%D1%82%20%D0%9A%D0%B0%D1%82%D1%8E%D1%88%D0%B8.wav",
            },
            {
                "tag": "Подарок",
                "title": "Подарочный трек - пример 1",
                "format": "WAV",
                "mime": "audio/wav",
                "url": "https://inrestart.com/portfoliomusic/Output%202.wav",
            },
            {
                "tag": "Подарок",
                "title": "Подарочный трек - пример 2",
                "format": "WAV",
                "mime": "audio/wav",
                "url": "https://inrestart.com/portfoliomusic/Podarok.wav",
            },
            {
                "tag": "Семья",
                "title": "Подарок для дочки",
                "format": "WAV",
                "mime": "audio/wav",
                "url": "https://inrestart.com/portfoliomusic/Podarok%D0%B4%D0%BB%D1%8F%D0%B4%D0%BE%D1%87%D0%BA%D0%B8.wav",
            },
            {
                "tag": "Подарок",
                "title": "Подарочный трек - пример 3",
                "format": "WAV",
                "mime": "audio/wav",
                "url": "https://inrestart.com/portfoliomusic/2.%20Podarok.wav",
            },
            {
                "tag": "Юбилей",
                "title": "Песня на юбилей Евгении",
                "format": "MP3",
                "mime": "audio/mpeg",
                "url": "https://inrestart.com/portfoliomusic/[%D0%AE%D0%B1%D0%B8%D0%BB%D0%B5%D0%B8%CC%86]%20%D0%95%D0%B2%D0%B3%D0%B5%D0%BD%D0%B8%D0%B8.mp3",
            },
        ],
    },

    "how": {
        "page_title": "Как это работает - Magic Music",
        "meta_description": "Как работает Magic Music: пошаговая анкета, 2 варианта текста песни, личный кабинет, оплата после выбора текста и выдача готового результата.",
        "eyebrow": "Как работает",
        "title": "Путь пользователя:",
        "lead": (
            "На этой странице собран весь сценарий сервиса: от первого шага до получения готовой песни. "
        ),
        "points": [
            "Пошаговая анкета",
            "2 версии текста",
            "Личный кабинет",
        ],
        "primary_label": "Создать песню",
        "primary_href": "/questionnaire/",
        "secondary_label": None,
        "secondary_href": None,
        "overlay_title": "Как работает",
        "overlay_items": [
            "история текстом или голосом",
            "сначала пользователь видит тексты",
            "оплата происходит только потом",
        ],
        "note": "Весь путь пользователя по шагам:",
        "steps": [
            {
                "num": "01",
                "title": "Пользователь открывает анкету",
                "text": "На первом шаге человек выбирает сценарий: написать песню по своей истории или прийти со своим готовым текстом.",
            },
            {
                "num": "02",
                "title": "Передаёт историю текстом или голосом",
                "text": "Историю можно написать вручную или просто записать голос прямо на сайте без отдельной загрузки файла.",
            },
            {
                "num": "03",
                "title": "Получает 2 варианта текста",
                "text": "Сервис генерирует два разных варианта, чтобы пользователь мог сравнить и выбрать тот, который ближе по смыслу и эмоции.",
            },
            {
                "num": "04",
                "title": "Сохраняет доступ через кабинет",
                "text": "После этого заказ закрепляется за пользователем, и дальнейшие статусы, тексты и результат становятся доступны в кабинете.",
            },
            {
                "num": "05",
                "title": "Переходит к оплате",
                "text": "Оплата происходит только после того, как человек уже увидел текст и понял, какой результат он получает.",
            },
            {
                "num": "06",
                "title": "Получает готовую песню",
                "text": "После оплаты заказ уходит в генерацию, а готовая песня в двух вариантах отобразится в личном кабинете.",
            },
        ],
    },
    
     "reviews": {
        "page_title": "Отзывы клиентов - Magic Music",
        "meta_description": "Отзывы клиентов Magic Music: реальные впечатления о песнях на юбилей, свадьбу, годовщину и личные подарки.",
        "eyebrow": "Кто уже получил песню",
        "title": "Отзывы клиентов",
        "lead": (
            "Здесь собраны живые отзывы клиентов после готового результата: на юбилей, годовщину, свадьбу, "
            "день рождения и другие важные поводы."
        ),
        "points": [
            "Реальные отзывы",
            "Разные поводы",
            "Эмоциональный результат",
        ],
        "primary_label": "Создать свою песню",
        "primary_href": "/questionnaire/",
        "secondary_label": None,
        "secondary_href": None,
        "overlay_title": "Отзывы",
        "overlay_items": [
            "юбилей",
            "годовщина свадьбы",
            "свадьба и день рождения",
        ],
        "note": "Отзывы клиентов без выдуманных историй и без шаблонных формулировок.",
        "reviews_list": [
            {
                "name": "Светлана",
                "badge": "Юбилей папе",
                "text": "Мне очень понравилась работа. Песня получилась просто супер. Даже не ожидала, что настолько можно передать песней свои чувства. Я заказывала песню на юбилей папе. Очень рекомендую. Быстро, качественно и по цене приемлемо. Благодарю вас за оказанную услугу.",
            },
            {
                "name": "Иван",
                "badge": "Годовщина свадьбы",
                "text": "Это просто бомба. Заказывал песню супруге на годовщину свадьбы. Сделали все на высшем уровне. Лучший подарок. Просто класс.",
            },
            {
                "name": "Олеся",
                "badge": "Подарок мужу",
                "text": "Я в полном восторге от этой песни! Она невероятно красивая, тёплая и искренняя. Музыка и слова идеально сочетаются, создавая особую атмосферу праздника и нежности. Мне очень приятно было услышать такие искренние эмоции, вложенные в каждую строчку. Эта песня станет для моего мужа ценным и трогательным подарком, который он обязательно запомнит навсегда. Большое спасибо за такой талант и душевное творчество!",
            },
            {
                "name": "Виктория",
                "badge": "Свадьба",
                "text": "Мастера на все 1000% из 100. Благодарю за выполненную работу в высочайшем качестве - в восторге были не только молодожены, но и все гости. Даже тамада, повидавшая не один подобный подарок, сказала, что чище и качественней работы не слышала.",
            },
            {
                "name": "Илья",
                "badge": "День рождения жены",
                "text": "Отлично справились с моей просьбой. Песня понравилась жене на день рождения - даже прослезилась, так растрогал текст. Все четко и в срок. Спасибо большое!",
            },
            {
                "name": "Елена",
                "badge": "Юбилей",
                "text": "Очень рада, что обратилась именно сюда для создания песен. Дважды составили прикольные и в то же время трогательные композиции. Спасибо огромное Евгении и техподдержке, когда были проблемы. Результат - ОГОНЬ, реакция гостей, а самое главное юбиляров, превзошла все ожидания. Обращусь к вам ещё раз!!!",
            },
            {
                "name": "Наталья",
                "badge": "Несколько вариантов",
                "text": "Все очень понравилось!!!! Помощь оказывали во всем!!!! По несколько часов делали песни!! И все 2 варианта - даже выше, чем 1000 баллов! Обращайтесь, не пожалеете, да и стоимость очень приятная!!!! Почти бесплатно!!! За такую услугу, которую оказали, можно и больше заплатить!!!! Миллион сердечек вам!!! Успехов и процветания!!!! От всей моей большой души! Это пожелание!!!",
            },
            {
                "name": "Алексей",
                "badge": "Повторное обращение",
                "text": "Очень понравился результат! Спасибо большое продавцу, что столько много уделил внимания! Буду обращаться ещё 😊 Ребята, вы волшебники👍👍👍",
            },
        ],
    },
}


LEGAL_PAGES = {
    "offer": {
        "page_title": "Оферта - Magic Music",
        "meta_description": "Публичная оферта Magic Music: условия оформления заказа, оплаты и получения персональной песни по истории пользователя.",
        "eyebrow": "Публичная оферта",
        "title": "Условия заказа и оказания услуги",
        "lead": "На этой странице описаны базовые условия оформления заказа в Magic Music: что входит в услугу, когда происходит оплата и как пользователь получает результат.",
        "updated_at": "Редакция от 14.03.2026",
        "sections": [
            {
                "title": "1. Общие положения",
                "paragraphs": [
                    "Настоящий текст является публичным предложением сервиса Magic Music оформить заказ на создание персональной песни по истории пользователя.",
                    "Использование сервиса, заполнение анкеты и оформление заказа означает согласие пользователя с описанным порядком работы сервиса."
                ],
                "bullet_items": [],
            },
            {
                "title": "2. Предмет услуги",
                "paragraphs": [
                    "Magic Music предоставляет цифровую услугу по созданию персональной песни на основе информации, которую пользователь передаёт через сайт.",
                ],
                "bullet_items": [
                    "приём истории в текстовом или голосовом формате;",
                    "генерация двух вариантов текста песни для выбора пользователем;",
                    "подготовка финальной песни после выбора текста и успешной оплаты;",
                    "предоставление доступа к статусу заказа и результату через личный кабинет.",
                ],
            },
            {
                "title": "3. Порядок оформления заказа",
                "paragraphs": [
                    "Пользователь проходит пошаговую анкету, передаёт историю или готовый текст, знакомится с двумя вариантами текста песни и выбирает подходящий вариант.",
                    "После этого пользователь указывает email для доступа к заказу и переходит к оплате. До оплаты пользователь видит текстовый результат и принимает решение о продолжении заказа."
                ],
                "bullet_items": [],
            },
            {
                "title": "4. Стоимость и оплата",
                "paragraphs": [
                    "Актуальная стоимость услуги отображается на сайте и на странице оплаты. Оплата принимается через YooKassa.",
                    "Формирование итоговой песни запускается после подтверждения оплаты платёжной системой."
                ],
                "bullet_items": [
                    "до оплаты пользователь получает 2 варианта текста песни;",
                    "оплата относится к этапу генерации готовой песни;",
                    "условия акций и специальных предложений применяются только в том виде, в котором они указаны на сайте на момент оформления заказа.",
                ],
            },
            {
                "title": "5. Сроки и результат",
                "paragraphs": [
                    "Срок подготовки результата зависит от текущей загрузки сервиса и технической доступности внешних AI-провайдеров.",
                    "Готовый результат размещается в личном кабинете пользователя. Если генерация требует дополнительного времени, статус заказа обновляется внутри кабинета."
                ],
                "bullet_items": [],
            },
            {
                "title": "6. Материалы пользователя",
                "paragraphs": [
                    "Пользователь отвечает за содержание отправленной истории, текста, имён, дат и других данных, переданных в сервис.",
                    "Отправляя материалы, пользователь подтверждает, что имеет право использовать их для создания персональной песни и что эти материалы не нарушают права третьих лиц."
                ],
                "bullet_items": [],
            },
            {
                "title": "7. Использование результата",
                "paragraphs": [
                    "Готовая песня предоставляется пользователю для личного использования в рамках оформленного заказа.",
                    "Если пользователю требуется публичное или коммерческое использование результата в особом режиме, такие условия лучше согласовывать отдельно до оплаты."
                ],
                "bullet_items": [],
            },
            {
                "title": "8. Ограничение ответственности",
                "paragraphs": [
                    "Сервис не несёт ответственность за невозможность оказания услуги по причинам, связанным с недостоверными данными пользователя, техническими сбоями внешних платёжных или AI-сервисов, а также перебоями связи и хостинга, которые не зависят от Magic Music.",
                    "Ответственность сервиса ограничивается стоимостью оплаченного заказа, если иное не предусмотрено обязательными нормами права."
                ],
                "bullet_items": [],
            },
            {
                "title": "9. Контакты",
                "paragraphs": [
                    "По вопросам заказа, доступа к кабинету и спорным ситуациям пользователь может обратиться через контакты, указанные на сайте Magic Music.",
                ],
                "bullet_items": [],
            },
        ],
    },
    "policy": {
        "page_title": "Политика конфиденциальности - Magic Music",
        "meta_description": "Политика конфиденциальности Magic Music: какие данные собираются, для чего используются и как обрабатываются в процессе создания персональной песни.",
        "eyebrow": "Политика конфиденциальности",
        "title": "Как Magic Music работает с данными пользователя",
        "lead": "На этой странице собрана базовая информация о том, какие данные пользователь передаёт через сайт Magic Music, зачем они нужны и как используются внутри сервиса.",
        "updated_at": "Редакция от 14.03.2026",
        "sections": [
            {
                "title": "1. Какие данные могут обрабатываться",
                "paragraphs": [
                    "При работе с сервисом пользователь может передавать данные, необходимые для создания песни и доступа к заказу.",
                ],
                "bullet_items": [
                    "имя и иные данные, указанные в анкете;",
                    "email для доступа к заказу и кабинету;",
                    "текст истории, пожелания, даты, имена и другие сведения, введённые пользователем;",
                    "голосовые сообщения, отправленные через встроенную запись на сайте;",
                    "технические данные, связанные с использованием сайта и оплатой заказа.",
                ],
            },
            {
                "title": "2. Для чего используются данные",
                "paragraphs": [
                    "Данные пользователя используются только в объёме, который нужен для работы сервиса и исполнения заказа.",
                ],
                "bullet_items": [
                    "создание и хранение заказа;",
                    "генерация двух вариантов текста песни и итогового результата;",
                    "распознавание голосового сообщения, если пользователь выбрал голосовой ввод;",
                    "предоставление доступа к кабинету и статусам заказа;",
                    "обработка оплаты и техническое сопровождение сервиса.",
                ],
            },
            {
                "title": "3. Передача данных третьим сторонам",
                "paragraphs": [
                    "Для выполнения заказа Magic Music может использовать внешние сервисы и подрядчиков, которые технически участвуют в работе платформы.",
                    "Это могут быть платёжные провайдеры, AI-сервисы для генерации текста и обработки голосового ввода, а также инфраструктурные сервисы хостинга и хранения данных."
                ],
                "bullet_items": [],
            },
            {
                "title": "4. Хранение и защита",
                "paragraphs": [
                    "Magic Music принимает разумные технические меры для защиты данных пользователя от несанкционированного доступа, утраты и случайного удаления.",
                    "При этом пользователь понимает, что передача данных через интернет не может гарантировать абсолютную защиту от всех возможных рисков."
                ],
                "bullet_items": [],
            },
            {
                "title": "5. Платёжные данные",
                "paragraphs": [
                    "Оплата заказа проходит через YooKassa. Magic Music не хранит полные данные банковских карт пользователя на своей стороне.",
                    "Параметры оплаты и подтверждение платежа обрабатываются по правилам платёжного провайдера."
                ],
                "bullet_items": [],
            },
            {
                "title": "6. Права пользователя",
                "paragraphs": [
                    "Пользователь вправе запросить уточнение, обновление или удаление своих данных в той части, в которой это не противоречит обязательствам по уже оформленному и оплаченному заказу, требованиям бухгалтерского учёта и технической необходимости хранения служебной информации.",
                ],
                "bullet_items": [],
            },
            {
                "title": "7. Изменение политики",
                "paragraphs": [
                    "Magic Music вправе обновлять настоящую политику по мере развития сервиса. Актуальная версия всегда публикуется на этой странице.",
                ],
                "bullet_items": [],
            },
            {
                "title": "8. Контакты",
                "paragraphs": [
                    "По вопросам обработки данных и доступа к заказу пользователь может обратиться через контакты, указанные на сайте Magic Music.",
                ],
                "bullet_items": [],
            },
        ],
    },
}

FAQ_PAGE = {
    "page_title": "FAQ - Magic Music",
    "meta_description": "FAQ Magic Music: ответы на частые вопросы о создании персональной песни, 2 версиях текста, оплате, кабинете и получении готового результата.",
    "eyebrow": "FAQ",
    "title": "Частые вопросы",
    "lead": "Собрали ответы на вопросы, которые чаще всего возникают перед созданием песни в Magic Music.",
    "updated_at": "Актуально на 18.03.2026",
    "sections": [
        {
            "title": "1. Как устроен заказ в Magic Music?",
            "paragraphs": [
                "Пользователь проходит пошаговую анкету, рассказывает историю текстом или голосом либо приходит со своим готовым текстом.",
                "Если история создаётся с нуля, сервис показывает 2 версии текста песни. После выбора подходящего варианта заказ сохраняется в личном кабинете, и только потом пользователь переходит к оплате."
            ],
            "bullet_items": [
                "историю можно передать текстом или голосом;",
                "до оплаты пользователь видит 2 варианта текста;",
                "после email заказ сохраняется в кабинете;",
                "готовая песня появляется после успешной оплаты и обработки заказа.",
            ],
        },
        {
            "title": "2. Когда происходит оплата?",
            "paragraphs": [
                "Оплата происходит не в самом начале, а после того, как пользователь уже увидел текстовый результат и выбрал дальнейший шаг.",
                "Это означает, что сначала человек знакомится с текстом, а уже потом принимает решение об оплате создания готовой песни."
            ],
            "bullet_items": [],
        },
        {
            "title": "3. Сколько вариантов текста я получу?",
            "paragraphs": [
                "Сейчас Magic Music показывает 2 варианта текста песни.",
                "Это сделано для того, чтобы можно было сравнить настроение, формулировки и выбрать более подходящий вариант перед оплатой."
            ],
            "bullet_items": [],
        },
        {
            "title": "4. Можно ли прийти со своим готовым текстом?",
            "paragraphs": [
                "Да. В анкете есть отдельная ветка, где можно сразу вставить готовый текст песни.",
                "В этом сценарии не нужно заново рассказывать историю — сервис работает с тем текстом, который пользователь уже подготовил."
            ],
            "bullet_items": [],
        },
        {
            "title": "5. Можно ли рассказать историю голосом?",
            "paragraphs": [
                "Да. В анкете есть вариант голосового ввода прямо на сайте.",
                "Это удобно, если историю проще надиктовать своими словами, чем писать вручную длинный текст."
            ],
            "bullet_items": [],
        },
        {
            "title": "6. Где потом смотреть заказ и статусы?",
            "paragraphs": [
                "После указания email заказ закрепляется за пользователем и открывается в личном кабинете.",
                "В кабинете можно вернуться к заказу, посмотреть выбранные параметры, статус оплаты и итоговый результат, когда он будет готов."
            ],
            "bullet_items": [],
        },
        {
            "title": "7. Что происходит после оплаты?",
            "paragraphs": [
                "После успешной оплаты заказ переходит в этап подготовки готовой песни.",
                "Дальше статус обновляется внутри кабинета, а результат появляется там же, без необходимости искать его в переписках или в сторонних сервисах."
            ],
            "bullet_items": [],
        },
        {
            "title": "8. Что делать, если что-то непонятно или возникла ошибка?",
            "paragraphs": [
                "Если на любом этапе возник вопрос по анкете, кабинету или оплате, лучше сразу написать в поддержку сервиса.",
                "Контакты для связи размещаются на сайте, чтобы можно было быстро уточнить ситуацию по конкретному заказу."
            ],
            "bullet_items": [],
        },
    ],
}

SUPPORT_PAGE = {
    "page_title": "Поддержка - Magic Music",
    "meta_description": "Поддержка Magic Music: форма обращения по анкете, кабинету, оплате и готовому заказу.",
    "eyebrow": "Поддержка",
    "title": "Связаться с поддержкой Magic Music",
    "lead": "Если возник вопрос по анкете, кабинету, оплате или готовому заказу, отправьте обращение через форму ниже.",
    "updated_at": "Актуально на 18.03.2026",
    "sections": [
        {
            "title": "По каким вопросам можно писать",
            "paragraphs": [
                "Поддержка помогает по шагам анкеты, доступу в кабинет, оплате, статусу заказа и проблемам с готовым результатом.",
                "Если что-то непонятно в текущем заказе, лучше написать сразу, чем пытаться пройти сценарий заново."
            ],
            "bullet_items": [
                "не приходит ссылка входа или не открывается кабинет;",
                "не получается пройти шаг анкеты;",
                "возник вопрос по оплате или статусу заказа;",
                "нужно уточнить, где смотреть готовый результат.",
            ],
        },
        {
            "title": "Что лучше сразу указать в сообщении",
            "paragraphs": [
                "Чтобы поддержка быстрее помогла, полезно сразу отправить максимум конкретики по ситуации.",
            ],
            "bullet_items": [
                "email, на который оформлялся заказ;",
                "ссылку на кабинет или номер заказа, если он уже есть;",
                "краткое описание проблемы;",
                "скриншот ошибки, если она отображается на сайте.",
            ],
        },
        {
            "title": "Как написать в поддержку",
            "paragraphs": [
                "Все обращения на этой странице отправляются через встроенную форму поддержки и сразу попадают в операторскую админку.",
                "Так мы быстрее связываем сообщение с заказом, видим историю обращения и можем точнее проверить статус."
            ],
            "bullet_items": [],
        },
        {
            "title": "Где после этого смотреть статус",
            "paragraphs": [
                "Если заказ уже сохранён за email, все основные изменения по нему лучше отслеживать в личном кабинете Magic Music.",
                "Поддержка помогает разобраться в спорных ситуациях, но сам статус заказа и итоговый результат появляются именно в кабинете."
            ],
            "bullet_items": [],
        },
    ],
}

def build_public_meta(path: str, page_title: str, meta_description: str) -> dict:
    base_url = settings.BASE_URL.rstrip("/")
    canonical_url = f"{base_url}{path}"
    og_image = f"{base_url}/static/img/hero-gift-song.jpg"

    return {
        "page_title": page_title,
        "meta_description": meta_description,
        "canonical_url": canonical_url,
        "og_title": page_title,
        "og_description": meta_description,
        "og_type": "website",
        "og_url": canonical_url,
        "og_image": og_image,
        "twitter_card": "summary_large_image",
    }


def normalize_support_order_ref(value: str | None) -> str:
    return (value or "").strip()


def find_order_for_support(db: Session, order_ref: str | None) -> Order | None:
    normalized = normalize_support_order_ref(order_ref)
    if not normalized:
        return None
    return db.query(Order).filter((Order.public_id == normalized) | (Order.order_number == normalized)).first()


def build_support_template_context(
    request: Request,
    *,
    order_ref: str = "",
    email: str = "",
    subject: str = "",
    message: str = "",
    error: str | None = None,
    success: str | None = None,
    thread_public_id: str | None = None,
    order: Order | None = None,
) -> dict:
    meta = build_public_meta(
        path=request.url.path,
        page_title=SUPPORT_PAGE["page_title"],
        meta_description=SUPPORT_PAGE.get("meta_description", ""),
    )
    return {
        "request": request,
        "support_page": SUPPORT_PAGE,
        "support_order_ref": order_ref,
        "support_email": email,
        "support_subject": subject,
        "support_message": message,
        "support_error": error,
        "support_success": success,
        "support_thread_public_id": thread_public_id,
        "support_order": order,
        "telegram_reporting_enabled": telegram_reporting_enabled(),
        **meta,
    }

def render_screen(request: Request, key: str):
    screen = SCREEN_PAGES[key]
    meta = build_public_meta(
        path=request.url.path,
        page_title=screen["page_title"],
        meta_description=screen["meta_description"],
    )

    return templates.TemplateResponse(
        "public/home.html",
        {
            "request": request,
            "screen": screen,
            "price_rub": settings.PRICE_RUB,
            **meta,
        },
    )


def render_legal_page(request: Request, key: str):
    legal_page = LEGAL_PAGES[key]
    meta = build_public_meta(
        path=request.url.path,
        page_title=legal_page["page_title"],
        meta_description=legal_page.get("meta_description", ""),
    )

    return templates.TemplateResponse(
        "public/legal.html",
        {
            "request": request,
            "legal_page": legal_page,
            **meta,
        },
    )


def render_faq_page(request: Request):
    meta = build_public_meta(
        path=request.url.path,
        page_title=FAQ_PAGE["page_title"],
        meta_description=FAQ_PAGE.get("meta_description", ""),
    )

    return templates.TemplateResponse(
        "public/legal.html",
        {
            "request": request,
            "legal_page": FAQ_PAGE,
            **meta,
        },
    )



def transliterate_slug(value: str) -> str:
    source = (value or "").strip().lower()
    if not source:
        return ""
    char_map = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
        "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    transliterated = "".join(char_map.get(char, char) for char in source)
    normalized = unicodedata.normalize("NFKD", transliterated)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_value = re.sub(r"[^a-z0-9\s-]", " ", ascii_value)
    ascii_value = re.sub(r"[\s_]+", "-", ascii_value)
    return re.sub(r"-{2,}", "-", ascii_value).strip("-")


def article_paragraphs(content_text: str) -> list[str]:
    normalized = (content_text or "").replace("\r\n", "\n")
    chunks = [item.strip() for item in normalized.split("\n\n")]
    return [item for item in chunks if item]


def blog_meta_context(request: Request, *, page_title: str, meta_description: str, path: str | None = None, og_image: str | None = None) -> dict:
    final_path = path or request.url.path
    context = build_public_meta(
        path=final_path,
        page_title=page_title,
        meta_description=meta_description,
    )
    if og_image:
        context["og_image"] = og_image
    return context


@router.get("/blog", response_class=HTMLResponse)
async def blog_page(
    request: Request,
    page: int = 1,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    per_page = 30
    page_no = max(page, 1)
    selected_category_slug = (category or "").strip().lower()

    categories = db.query(BlogCategory).order_by(BlogCategory.name.asc()).all()
    selected_category = next((item for item in categories if item.slug == selected_category_slug), None)

    query = db.query(BlogArticle).filter(BlogArticle.is_published.is_(True))
    if selected_category:
        query = query.filter(BlogArticle.category_id == selected_category.id)

    total_count = query.count()
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1
    if page_no > total_pages:
        page_no = total_pages

    articles = (
        query.order_by(BlogArticle.created_at.desc(), BlogArticle.id.desc())
        .offset((page_no - 1) * per_page)
        .limit(per_page)
        .all()
    )

    category_stats = []
    for category_item in categories:
        count = db.query(BlogArticle).filter(
            BlogArticle.is_published.is_(True),
            BlogArticle.category_id == category_item.id,
        ).count()
        category_stats.append({"category": category_item, "count": count})

    page_title = "Блог — Magic Music"
    if selected_category:
        page_title = f"Блог: {selected_category.name} — Magic Music"
    meta_description = "Блог Magic Music: свежие статьи, советы и идеи о персональных песнях и музыкальных подарках."

    return templates.TemplateResponse(
        "public/blog.html",
        {
            "request": request,
            "articles": articles,
            "categories": category_stats,
            "selected_category": selected_category,
            "selected_category_slug": selected_category_slug,
            "page_no": page_no,
            "total_pages": total_pages,
            "page_title": page_title,
            "meta_keywords": "блог, персональная песня, песня в подарок, музыка, идеи подарков",
            "hero_cta_url": "/questionnaire/",
            **blog_meta_context(request, page_title=page_title, meta_description=meta_description),
        },
    )


@router.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_article_page(slug: str, request: Request, db: Session = Depends(get_db)):
    article = (
        db.query(BlogArticle)
        .filter(BlogArticle.slug == slug.strip().lower(), BlogArticle.is_published.is_(True))
        .first()
    )
    if article is None:
        raise HTTPException(status_code=404)

    paragraphs = article_paragraphs(article.content_text)
    midpoint = max(1, len(paragraphs) // 2) if paragraphs else 1
    og_image = None
    if article.hero_image_path:
        og_image = f"{settings.BASE_URL.rstrip('/')}" + article.hero_image_path
    return templates.TemplateResponse(
        "public/blog_article.html",
        {
            "request": request,
            "article": article,
            "paragraphs": paragraphs,
            "midpoint": midpoint,
            "hero_cta_url": "/questionnaire/",
            "meta_keywords": article.meta_keywords or "блог, персональная песня, песня в подарок",
            **blog_meta_context(
                request,
                page_title=article.meta_title or f"{article.title} — Magic Music",
                meta_description=article.meta_description or article.excerpt or article.title,
                path=f"/blog/{article.slug}",
                og_image=og_image,
            ),
        },
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return render_screen(request, "home")


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return render_screen(request, "portfolio")


@router.get("/how-it-works", response_class=HTMLResponse)
async def how_it_works_page(request: Request):
    return render_screen(request, "how")


@router.get("/reviews", response_class=HTMLResponse)
async def reviews_page(request: Request):
    return render_screen(request, "reviews")


@router.get("/offer", response_class=HTMLResponse)
async def offer_page(request: Request):
    return render_legal_page(request, "offer")


@router.get("/policy", response_class=HTMLResponse)
async def policy_page(request: Request):
    return render_legal_page(request, "policy")


@router.get("/faq", response_class=HTMLResponse)
async def faq_page(request: Request):
    return render_faq_page(request)


@router.get("/support", response_class=HTMLResponse)
async def support_page():
    return RedirectResponse(url="/", status_code=303)


@router.post("/support", response_class=HTMLResponse)
async def support_page_submit():
    return RedirectResponse(url="/", status_code=303)


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    base_url = settings.BASE_URL.rstrip("/")
    return PlainTextResponse(
        "\n".join(
            [
                "User-agent: *",
                "Allow: /",
                "",
                f"Sitemap: {base_url}/sitemap.xml",
            ]
        )
    )


@router.get("/sitemap.xml")
async def sitemap_xml():
    base_url = settings.BASE_URL.rstrip("/")
    urls = [
        f"{base_url}/",
        f"{base_url}/portfolio",
        f"{base_url}/how-it-works",
        f"{base_url}/reviews",
        f"{base_url}/blog",
        f"{base_url}/offer",
        f"{base_url}/policy",
        f"{base_url}/faq",
    ]
    with Session(engine) as db:
        blog_slugs = [
            item[0]
            for item in db.query(BlogArticle.slug)
            .filter(BlogArticle.is_published.is_(True))
            .all()
        ]
    urls.extend(f"{base_url}/blog/{slug}" for slug in blog_slugs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(
            f"<url><loc>{url}</loc></url>"
            for url in urls
        )
        + "</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


def _check_database_health() -> tuple[bool, str | None]:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _check_redis_health() -> tuple[bool, str | None]:
    try:
        get_redis_connection().ping()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _detect_storage_mode() -> str:
    if (settings.OBJECT_STORAGE_BUCKET or "").strip() and (settings.OBJECT_STORAGE_ACCESS_KEY_ID or "").strip() and (settings.OBJECT_STORAGE_SECRET_ACCESS_KEY or "").strip():
        return "object_storage"
    return "local"


@router.get("/health")
async def health():
    return {
        "ok": True,
        "service": "magic-music-web",
        "queue_name": settings.BACKGROUND_QUEUE_NAME,
        "background_jobs_sync_mode": settings.BACKGROUND_JOBS_SYNC_MODE,
        "storage_mode": _detect_storage_mode(),
    }


@router.get("/ready")
async def ready():
    db_ok, db_error = _check_database_health()
    redis_ok, redis_error = _check_redis_health()

    checks = {
        "database": {"ok": db_ok, "error": db_error},
        "redis": {"ok": redis_ok, "error": redis_error},
        "storage": {"mode": _detect_storage_mode()},
    }
    ok = db_ok and redis_ok
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "ok": ok,
            "service": "magic-music-web",
            "checks": checks,
        },
    )
