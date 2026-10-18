import re

import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import logging

import database
import ai_core

logger = logging.getLogger(__name__)

MONTH_NAMES_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
}

MONTH_URLS = {
    1: "yanvar", 2: "fevral", 3: "mart", 4: "aprel",
    5: "may", 6: "iyun", 7: "iyul", 8: "avgust",
    9: "sentyabr", 10: "oktyabr", 11: "noyabr", 12: "dekabr"
}

# Базовые праздники РФ (на случай недоступности внешних сайтов)
BUILTIN_RU_HOLIDAYS = {
    (1, 1): ["Новый год 🎄"],
    (7, 1): ["Рождество Христово ✨"],
    (14, 2): ["День святого Валентина ❤️", "День компьютерщика 💻"],
    (23, 2): ["День защитника Отечества 🛡"],
    (8, 3): ["Международный женский день 🌷"],
    (1, 4): ["День смеха 🤡"],
    (12, 4): ["День космонавтики 🚀"],
    (1, 5): ["Праздник Весны и Труда 🛠"],
    (7, 5): ["День радио (праздник всех отраслей связи) 📻"],
    (9, 5): ["День Победы 🎗"],
    (12, 6): ["День России 🇷🇺"],
    (1, 9): ["День знаний 📚"],
    (5, 10): ["День учителя 🎓"],
    (4, 11): ["День народного единства 🤝"],
    (30, 11): ["Международный день защиты информации (День ИБ) 🔐"],
    (31, 12): ["Канун Нового года 🥂"],
}


def get_floating_holidays(dt: datetime) -> list[str]:
    """Вычисляет переходящие праздники РФ (День программиста, День сисадмина и др.)"""
    res = []
    # День программиста — 256-й день года
    if dt.timetuple().tm_yday == 256:
        res.append("День программиста 👨‍💻")
    # День сисадмина — последняя пятница июля
    if dt.month == 7 and dt.weekday() == 4 and (dt + timedelta(days=7)).month == 8:
        res.append("День системного администратора 🍻🖥")
    # День тестировщика — 9 сентября
    if dt.day == 9 and dt.month == 9:
        res.append("День тестировщика 🐞")
    return res


async def fetch_web_holidays(dt: datetime, max_items: int = 8) -> list[str]:
    """Парсит реальные праздники РФ с kakoysegodnyaprazdnik.ru на указанную дату"""
    m_slug = MONTH_URLS.get(dt.month)
    url = f"https://kakoysegodnyaprazdnik.ru/baza/{m_slug}/{dt.day}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
    }
    found = []
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=8) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    soup = BeautifulSoup(html_text, "html.parser")
                    for span in soup.select('span[itemprop="text"]'):
                        txt = span.get_text(strip=True)
                        if txt and txt not in found:
                            found.append(txt)
                        if len(found) >= max_items:
                            break
    except Exception as e:
        logger.warning(f"Не удалось спарсить праздники с сайта на {dt.strftime('%d.%m')}: {e}")

    # Объединяем с нашими точными переходящими и базовыми праздниками
    builtin = BUILTIN_RU_HOLIDAYS.get((dt.day, dt.month), []) + get_floating_holidays(dt)
    for b in reversed(builtin):
        if not any(b.split()[0].lower() in f.lower() for f in found):
            found.insert(0, b)

    return found[:max_items]


async def build_today_holidays_message(chat_id: int, dt: datetime = None) -> str:
    """Формирует полный ответ о праздниках и ДР на сегодня (по слову 'праздники')"""
    if dt is None:
        dt = datetime.now()

    date_str = f"{dt.day} {MONTH_NAMES_GEN[dt.month]}"
    custom = await database.get_custom_events_by_date(chat_id, dt.day, dt.month)
    web_list = await fetch_web_holidays(dt, max_items=8)

    custom_lines = []
    for ev in custom:
        icon = "🎂 День рождения:" if ev["category"] == "birthday" else "⭐ Особый день:"
        custom_lines.append(f"{icon} <b>{ev['title']}</b>")

    # Запасной прямой текст (на случай если все ИИ перегружены или вернули пустоту)
    fallback_text = f"📅 <b>Праздники сегодня ({date_str}):</b>\n\n"
    if custom_lines:
        fallback_text += "🎉 <b>События нашего чата:</b>\n" + "\n".join(f"• {c}" for c in custom_lines) + "\n\n"
    if web_list:
        fallback_text += "🌍 <b>Праздники в этот день:</b>\n" + "\n".join(f"• {w}" for w in web_list)
    else:
        fallback_text += "На сегодня особых праздников в календаре не найдено."

    facts_block = f"Дата: {date_str}.\n"
    if custom_lines:
        facts_block += "События нашего чата сегодня:\n" + "\n".join(f"- {c}" for c in custom_lines) + "\n\n"
    if web_list:
        facts_block += "Официальные и народные праздники сегодня:\n" + "\n".join(f"- {w}" for w in web_list)
    else:
        facts_block += "Список с сайта недоступен, назови 3-4 известных праздника в России на эту дату."

    prompt = (
        f"Расскажи участникам чата, какие сегодня праздники ({date_str}).\n"
        f"Вот точные данные:\n{facts_block}\n\n"
        "Правила:\n"
        "1. Если есть 'События нашего чата' — начни с них и тепло поздравь!\n"
        "2. Перечисли 4-6 самых интересных праздников из списка с подходящими эмодзи.\n"
        "3. В конце добавь короткое бодрое пожелание на день. Отвечай ТОЛЬКО текстом, НЕ используй тег [REACTION]."
    )

    try:
        reply = await ai_core.ask_llm(chat_id, prompt, chat_type="group", save_to_history=False)
        clean_reply = re.sub(r'\[REACTION:\s*.*?\]\s*', '', reply or "").strip()
        if clean_reply and not clean_reply.startswith("⚠️"):
            return clean_reply
    except Exception as e:
        logger.warning(f"ИИ не смог оформить праздники, отдаю прямой список: {e}")

    return fallback_text


def _format_holidays_Mode_label(mode: int) -> str:
    if mode == 1:
        return "🟡 Праздники: ТОЛЬКО СВОИ (ДР)"
    elif mode == 2:
        return "🟢 Праздники: ВСЕ (КАЖДЫЙ ДЕНЬ)"
    return "🔴 Праздники и ДР: ВЫКЛ"


async def build_monday_digest(chat_id: int, start_dt: datetime = None) -> str:
    """Краткий дайджест на понедельник: только главные праздники (по 1 на день) + все свои события"""
    if start_dt is None:
        start_dt = datetime.now()

    all_custom = await database.get_custom_events(chat_id)
    week_lines = []
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    for i in range(7):
        cur = start_dt + timedelta(days=i)
        day_label = f"{days_ru[cur.weekday()]} ({cur.day:02d}.{cur.month:02d})"

        day_custom = [
            f"{'🎂 ДР: ' if e['category']=='birthday' else '⭐ '}{e['title']}"
            for e in all_custom if e["day"] == cur.day and e["month"] == cur.month
        ]
        # Берем только 1 главный общий праздник, чтобы сообщение было коротким
        day_web = await fetch_web_holidays(cur, max_items=1)

        combined = day_custom + day_web
        if combined:
            week_lines.append(f"• <b>{day_label}</b>: " + "; ".join(combined))

    facts = "\n".join(week_lines)
    fallback_digest = f"🗓 <b>Коротко о главном на этой неделе:</b>\n\n{facts}"

    prompt = (
        "Сегодня понедельник! Напиши для чата ОЧЕНЬ КРАТКИЙ и бодрый обзор главных праздников и событий на эту неделю.\n"
        f"Данные по дням:\n{facts}\n\n"
        "Правила:\n"
        "1. Пиши максимально лаконично (по 1 короткой строчке на день).\n"
        "2. Если есть Дни рождения (🎂) или особые даты чата (⭐) — выдели их в самом начале!\n"
        "3. Отвечай ТОЛЬКО текстом, НЕ используй тег [REACTION]."
    )

    try:
        reply = await ai_core.ask_llm(chat_id, prompt, chat_type="group", save_to_history=False)
        clean_reply = re.sub(r'\[REACTION:\s*.*?\]\s*', '', reply or "").strip()
        if clean_reply and not clean_reply.startswith("⚠️"):
            return clean_reply
    except Exception as e:
        logger.warning(f"ИИ не смог оформить дайджест недели, отдаю прямой список: {e}")

    return fallback_digest


async def build_friday_preview(chat_id: int, friday_dt: datetime = None) -> str | None:
    """Пятничный анонс: проверяет следующую неделю (Пн-Вс). Если своих ДР/событий нет — возвращает None."""
    if friday_dt is None:
        friday_dt = datetime.now()

    all_custom = await database.get_custom_events(chat_id)
    if not all_custom:
        return None

    # Следующий понедельник = пятница + 3 дня
    next_monday = friday_dt + timedelta(days=3)
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    upcoming_custom = []

    for i in range(7):
        cur = next_monday + timedelta(days=i)
        day_label = f"{days_ru[cur.weekday()]} ({cur.day:02d}.{cur.month:02d})"
        for e in all_custom:
            if e["day"] == cur.day and e["month"] == cur.month:
                icon = "🎂 День рождения:" if e["category"] == "birthday" else "⭐ Особое событие:"
                upcoming_custom.append(f"• <b>{day_label}</b> — {icon} {e['title']}")

    # Если на следующей неделе нет внутренних событий или ДР — ничего не пишем
    if not upcoming_custom:
        return None

    events_block = "\n".join(upcoming_custom)
    fallback_text = (
        "🍻 <b>Рабочая неделя подходит к концу!</b>\n\n"
        "🔔 Напоминаю, какие важные даты ждут нас на следующей неделе:\n"
        f"{events_block}"
    )

    prompt = (
        "Сегодня пятница, рабочая неделя подходит к концу! "
        "Поздравь чат с пятницей в 1 предложении и напомни ребятам, какие Дни рождения и особые события нашего чата "
        f"ждут нас на следующей неделе:\n{events_block}\n\n"
        "Пиши коротко, тепло и с эмодзи. НЕ используй тег [REACTION]."
    )

    try:
        reply = await ai_core.ask_llm(chat_id, prompt, chat_type="group", save_to_history=False)
        clean_reply = re.sub(r'\[REACTION:\s*.*?\]\s*', '', reply or "").strip()
        if clean_reply and not clean_reply.startswith("⚠️"):
            return clean_reply
    except Exception as e:
        logger.warning(f"Ошибка ИИ в пятничном анонсе: {e}")

    return fallback_text