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

# Кэш производственного календаря РФ в памяти: {год: данные_json}
_WORK_CALENDAR_CACHE: dict[int, dict] = {}


def md_to_html(text: str) -> str:
    """Очищает ответ от тегов реакций и конвертирует случайный Markdown (**bold**) в HTML (<b>bold</b>)"""
    if not text:
        return ""
    text = re.sub(r'\[REACTION:\s*.*?\]\s*', '', text).strip()
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    return text


async def _get_ru_work_calendar(year: int) -> dict | None:
    """
    Загружает официальный производственный календарь РФ на нужный год с xmlcalendar.ru
    и кэширует его в оперативной памяти.
    """
    if year in _WORK_CALENDAR_CACHE:
        return _WORK_CALENDAR_CACHE[year]

    url = f"https://xmlcalendar.ru/data/ru/{year}/calendar.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    _WORK_CALENDAR_CACHE[year] = data
                    logger.info(f"Производственный календарь РФ на {year} год успешно загружен ✅")
                    return data
    except Exception as e:
        logger.warning(f"Не удалось загрузить календарь с xmlcalendar.ru ({year}): {e}")

    return None


async def check_workday_calendar_status(dt: datetime) -> str | None:
    """
    Динамически проверяет дату по производственному календарю РФ (с учетом всех переносов):
    - Выходной праздничный или перенесенный день среди недели (Пн-Пт)
    - Сокращенный предпраздничный рабочий день
    - Рабочая суббота/воскресенье (когда выходной перенесли на другой день)
    """
    cal = await _get_ru_work_calendar(dt.year)
    if cal:
        # Названия официальных праздников по их ID: {"1": "Новогодние каникулы", ...}
        holidays_map = {str(h.get("id")): h.get("title", "") for h in cal.get("holidays", [])}

        for m in cal.get("months", []):
            if m.get("month") == dt.month:
                # В xmlcalendar дни записаны через запятую: "1,2,3*,4+,8*"
                # где: без знака = выходной/праздник, '*' = сокращенный день, '+' = перенесенный выходной
                raw_days = [d.strip() for d in m.get("days", "").split(",") if d.strip()]
                for token in raw_days:
                    day_num_str = re.sub(r'[^\d]', '', token)
                    if not day_num_str or int(day_num_str) != dt.day:
                        continue

                    # 1. Сокращенный предпраздничный день (помечен '*')
                    if "*" in token:
                        return "⏰ Сокращенный предпраздничный рабочий день (на 1 час короче)"

                    # 2. Если это будний день (Пн-Пт) и он есть в списке особых дней без '*' — значит мы ОТДЫХАЕМ!
                    if dt.weekday() < 5:
                        if "+" in token:
                            return "🏖 Официальный нерабочий выходной (перенос выходного дня)"
                        return "🏖 Официальный праздничный выходной день"

        # Проверяем, не сделали ли субботу/воскресенье рабочей:
        # В xmlcalendar обычные выходные входят в m["days"]. Если субботы/воскресенья там НЕТ — это рабочая суббота!
        if dt.weekday() >= 5:
            for m in cal.get("months", []):
                if m.get("month") == dt.month:
                    raw_days = [re.sub(r'[^\d]', '', d) for d in m.get("days", "").split(",")]
                    if str(dt.day) not in raw_days:
                        return "⚠️ Рабочий выходной день (по производственному календарю РФ)"

    # Резервный фолбэк через API isdayoff.ru (если xmlcalendar недоступен)
    url = f"https://isdayoff.ru/{dt.strftime('%Y%m%d')}?cc=ru&pre=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=4) as resp:
                if resp.status == 200:
                    code = (await resp.text()).strip()
                    if code == "2":
                        return "⏰ Сокращенный предпраздничный рабочий день"
                    if dt.weekday() < 5 and code == "1":
                        return "🏖 Официальный нерабочий выходной день (по производственному календарю РФ)"
                    if dt.weekday() >= 5 and code == "0":
                        return "⚠️ Рабочая суббота/воскресенье (перенос выходного дня)"
    except Exception:
        pass

    return None


async def fetch_web_holidays(dt: datetime, max_items: int = 8) -> list[str]:
    """Парсит список всех праздников дня (официальных, профессиональных, народных) с сайта"""
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

    return found[:max_items]


async def build_today_holidays_message(chat_id: int, dt: datetime = None) -> str:
    """Формирует полный ответ о праздниках и ДР на сегодня (по слову 'праздники' или в режиме 2)"""
    if dt is None:
        dt = datetime.now()

    date_str = f"{dt.day} {MONTH_NAMES_GEN[dt.month]}"
    custom = await database.get_custom_events_by_date(chat_id, dt.day, dt.month)
    web_list = await fetch_web_holidays(dt, max_items=8)
    work_status = await check_workday_calendar_status(dt)

    custom_lines = []
    for ev in custom:
        icon = "🎂 День рождения:" if ev["category"] == "birthday" else "⭐ Особый день:"
        custom_lines.append(f"{icon} <b>{ev['title']}</b>")

    fallback_text = f"📅 <b>Праздники сегодня ({date_str}):</b>\n\n"
    if work_status:
        fallback_text += f"{work_status}\n\n"
    if custom_lines:
        fallback_text += "🎉 <b>События нашего чата:</b>\n" + "\n".join(f"• {c}" for c in custom_lines) + "\n\n"
    if web_list:
        fallback_text += "🌍 <b>Праздники в этот день:</b>\n" + "\n".join(f"• {w}" for w in web_list)
    else:
        fallback_text += "Не удалось загрузить список праздников с внешнего календаря."

    facts_block = f"Дата: {date_str}.\n"
    if work_status:
        facts_block += f"Статус дня по производственному календарю РФ: {work_status}\n"
    if custom_lines:
        facts_block += "События нашего чата сегодня:\n" + "\n".join(f"- {c}" for c in custom_lines) + "\n\n"
    if web_list:
        facts_block += "Праздники сегодня:\n" + "\n".join(f"- {w}" for w in web_list)

    prompt = (
        f"Расскажи участникам чата, какие сегодня праздники ({date_str}).\n"
        f"Вот точные данные из календаря:\n{facts_block}\n\n"
        "Правила:\n"
        "1. Если есть 'События нашего чата' — начни с них и тепло поздравь! Если их нет — НЕ пиши фразу 'именинников сегодня нет', сразу переходи к праздникам.\n"
        "2. Если указан особый статус по производственному календарю (выходной, перенос или сокращенный день) — обязательно упомяни это!\n"
        "3. Перечисли 4-5 самых интересных праздников на этот день с эмодзи.\n"
        "4. Используй ТОЛЬКО HTML-теги <b>...</b> для жирного шрифта (ЗАПРЕЩЕНО использовать **звездочки** и [REACTION])."
    )

    try:
        reply = await ai_core.ask_llm(chat_id, prompt, chat_type="group", save_to_history=False)
        clean_reply = md_to_html(reply)
        if clean_reply and not clean_reply.startswith("⚠️"):
            return clean_reply
    except Exception as e:
        logger.warning(f"ИИ не смог оформить праздники, отдаю прямой список: {e}")

    return fallback_text


async def build_monday_digest(chat_id: int, start_dt: datetime = None) -> str | None:
    """
    Дайджест понедельника: выводит ТОЛЬКО запланированные ДР/события чата
    и изменения графика работы по производственному календарю РФ (дополнительные выходные, переносы, сокращенные дни, рабочие субботы).
    Если ничего нет — возвращает None.
    """
    if start_dt is None:
        start_dt = datetime.now()

    all_custom = await database.get_custom_events(chat_id)
    days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

    planned_lines = []
    work_calendar_lines = []

    for i in range(7):
        cur = start_dt + timedelta(days=i)
        day_label = f"{days_ru[cur.weekday()]} ({cur.day:02d}.{cur.month:02d})"

        # 1. Свои ДР и особые события чата из БД
        for e in all_custom:
            if e["day"] == cur.day and e["month"] == cur.month:
                icon = "🎂 День рождения:" if e["category"] == "birthday" else "⭐ Особое событие:"
                planned_lines.append(f"• <b>{day_label}</b> — {icon} <b>{e['title']}</b>")

        # 2. Производственный календарь РФ (выходные среди недели, сокращенные дни, рабочие субботы)
        work_status = await check_workday_calendar_status(cur)
        if work_status:
            work_calendar_lines.append(f"• <b>{day_label}</b> — {work_status}")

    # Если на неделе нет ни своих событий, ни особых дней в производственном календаре — молчим
    if not planned_lines and not work_calendar_lines:
        return None

    facts = ""
    if planned_lines:
        facts += "🎉 <b>События и Дни рождения нашего чата на этой неделе:</b>\n" + "\n".join(planned_lines) + "\n\n"
    if work_calendar_lines:
        facts += "🗓 <b>Производственный календарь на неделю:</b>\n" + "\n".join(work_calendar_lines) + "\n\n"

    fallback_digest = f"📅 <b>Важные события на эту неделю:</b>\n\n{facts.strip()}"

    prompt = (
        "Сегодня понедельник! Напиши для чата короткий бодрый анонс важных событий на начавшуюся неделю.\n"
        f"Вот СТРОГИЙ список событий:\n{facts}\n"
        "ЖЕСТКИЕ ПРАВИЛА:\n"
        "1. Упоминай ТОЛЬКО те дни и события, которые переданы в списке выше!\n"
        "2. СТРОГО ЗАПРЕЩЕНО расписывать пустые дни недели и придумывать от себя 'день продуктивности', 'экватор недели' и т.д.\n"
        "3. Если есть официальные выходные, сокращенные дни или рабочие субботы по производственному календарю — обязательно предупреди об этом участников!\n"
        "4. Для выделения используй HTML-теги <b>...</b> (не используй **звездочки** и не используй [REACTION])."
    )

    try:
        reply = await ai_core.ask_llm(chat_id, prompt, chat_type="group", save_to_history=False)
        clean_reply = md_to_html(reply)
        if clean_reply and not clean_reply.startswith("⚠️"):
            return clean_reply
    except Exception as e:
        logger.warning(f"ИИ не смог оформить дайджест недели, отдаю прямой список: {e}")

    return fallback_digest


async def build_friday_preview(chat_id: int, friday_dt: datetime = None) -> str | None:
    """Пятничный анонс: проверяет следующую неделю (Пн-Вс). Если своих ДР/событий или переносов в календаре нет — возвращает None."""
    if friday_dt is None:
        friday_dt = datetime.now()

    all_custom = await database.get_custom_events(chat_id)
    next_monday = friday_dt + timedelta(days=3)
    days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    upcoming_items = []

    for i in range(7):
        cur = next_monday + timedelta(days=i)
        day_label = f"{days_ru[cur.weekday()]} ({cur.day:02d}.{cur.month:02d})"
        for e in all_custom:
            if e["day"] == cur.day and e["month"] == cur.month:
                icon = "🎂 День рождения:" if e["category"] == "birthday" else "⭐ Особое событие:"
                upcoming_items.append(f"• <b>{day_label}</b> — {icon} <b>{e['title']}</b>")

        work_status = await check_workday_calendar_status(cur)
        if work_status:
            upcoming_items.append(f"• <b>{day_label}</b> — {work_status}")

    if not upcoming_items:
        return None

    events_block = "\n".join(upcoming_items)
    fallback_text = (
        "🍻 <b>Рабочая неделя подходит к концу!</b>\n\n"
        "🔔 Напоминаю, какие важные даты и изменения графика ждут нас на следующей неделе:\n"
        f"{events_block}"
    )

    prompt = (
        "Сегодня пятница, рабочая неделя подходит к концу! "
        "Поздравь чат с пятницей в 1 предложении и перечисли СТРОГО по списку, какие события и изменения рабочего календаря "
        f"ждут нас на следующей неделе:\n{events_block}\n\n"
        "Пиши коротко, не выдумывай лишних дней. Используй HTML-теги <b>...</b> без **звездочек** и без [REACTION]."
    )

    try:
        reply = await ai_core.ask_llm(chat_id, prompt, chat_type="group", save_to_history=False)
        clean_reply = md_to_html(reply)
        if clean_reply and not clean_reply.startswith("⚠️"):
            return clean_reply
    except Exception as e:
        logger.warning(f"Ошибка ИИ в пятничном анонсе: {e}")

    return fallback_text