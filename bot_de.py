# -*- coding: utf-8 -*-

from __future__ import annotations

import io, os, re, logging
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo
from decimal import Decimal
from statistics import median

from weasyprint import HTML
from jinja2 import Environment, FileSystemLoader
from dateutil.relativedelta import relativedelta

from PIL import Image as PILImage
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing, Rect, Circle

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# Если в оригинале использовался PyPDF2 / pypdf для нотариуса:
try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    pass

# ---- logging
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("abako-it")

# ---- reportlab
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, KeepTogether
)
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

BASE_DIR = Path(__file__).resolve().parent

# ---------- TIME ----------
TZ_IT = ZoneInfo("Europe/Rome")


def now_it_date() -> str:
    return datetime.now(TZ_IT).strftime("%d.%m.%Y")


# ---------- FONTS ----------
try:
    pdfmetrics.registerFont(TTFont("PTMono", "fonts/PTMono-Regular.ttf"))
    pdfmetrics.registerFont(TTFont("PTMono-Bold", "fonts/PTMono-Bold.ttf"))
    F_MONO = "PTMono"
    F_MONO_B = "PTMono-Bold"
except Exception:
    F_MONO = "Courier"
    F_MONO_B = "Courier-Bold"

# ---------- COMPANY / CONSTANTS ----------
COMPANY = {
    "brand": "Abako",
    "legal": "ABAKO S.R.L.",
    "addr": "Strada Statale 16 Adriatica 8, 61121 Pesaro (PU), Italia",
    "reg": "Numero Iscrizione: A15173; Codice Fiscale: 02777020419",
    "contact": "Sito Web: abako-prestiti.it",
    "email": "info@abako-prestiti.it",
    "web": "abako-prestiti.it",
    "business_scope": (
        "Mediazione creditizia e consulenza finanziaria. "
        "Gestione pratiche di finanziamento e prestiti personali in qualità di intermediario. "
        "Servizi di consulenza e analisi del merito creditizio."
    ),
}

# ---------- BANK PROFILES ----------
BANKS = {
    "IT": {
        "name": "ING Bank N.V. Milan Branch",
        "addr": "Viale Fulvio Testi 250, 20126 Milano (MI), Italia",
        "piva": "11241140158"
    },
}


def get_bank_profile(cc: str) -> dict:
    return BANKS.get(cc.upper(), BANKS["IT"])


def asset_path(*candidates: str) -> str:
    roots = [BASE_DIR / "assets", BASE_DIR, Path.cwd() / "assets", Path.cwd()]
    env_dir = os.getenv("ASSETS_DIR")
    if env_dir:
        roots.insert(0, Path(env_dir))
    roots.append(Path("/mnt/data"))

    for name in candidates:
        for root in roots:
            p = (root / name).resolve()
            if p.exists():
                return str(p)
    return str((BASE_DIR / "assets" / candidates[0]).resolve())


# ---------- ASSETS ----------
ASSETS = {
    "logo_partner1": asset_path("ing_logo.png", "SANTANDER1.PNG"),
    "logo_partner2": asset_path("santander2.png", "SANTANDER2.PNG"),
    "logo_santa": asset_path("santa.png", "SANTA.PNG",),
    "logo_higobi": asset_path("HIGOBI_LOGO.PNG", "higobi_logo.png"),
    "sign_bank": asset_path("wagnersign.png", "wagnersign.PNG"),
    "sign_c2g": asset_path("duraksign.png", "duraksign.PNG"),
    "stamp_santa": asset_path("santastamp.png", "SANTASTAMP.PNG"),
    "sign_kirk": asset_path("kirk.png", "KIRK.PNG"),
    "notary_pdf": asset_path("notary_template.pdf"),
}

# ---------- UI ----------
BTN_CONTRACT = "Создать контракт"
BTN_CARD = "Выдача на карту"
BTN_NOTARY = "Редактировать нотариальное заверение (PDF)"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_CONTRACT), KeyboardButton(BTN_CARD)],
        [KeyboardButton(BTN_NOTARY)],
    ],
    resize_keyboard=True,
)

# ---------- STATES ----------
(ASK_CLIENT, ASK_AMOUNT, ASK_TAN, ASK_TERM) = range(20, 24)
ASK_FEE = 25
(CARD_NAME, CARD_ADDR) = range(300, 302)
ASK_NOTARY_AMOUNT = 410


# ---------- HELPERS ----------
def fmt_eur_it_with_cents(v):
    if isinstance(v, Decimal): v = float(v)
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def parse_num(txt: str) -> float:
    t = txt.strip().replace(" ", "").replace(".", "").replace(",", ".")
    return float(t)
# Псевдоним для форматирования валюты, чтобы работал старый код
def fmt_eur(v):
    return fmt_eur_it_with_cents(v)
def calculate_amortization_schedule(principal: float, tan_percent: float, months: int):
    if months <= 0 or principal <= 0:
        return 0, 0, []

    monthly_rate = (tan_percent / 100.0) / 12.0
    if monthly_rate == 0:
        annuity = principal / months
    else:
        annuity = principal * (monthly_rate / (1 - (1 + monthly_rate) ** (-months)))

    schedule = []
    remaining_balance = principal
    total_interest_paid = 0.0
    current_date = date.today() + relativedelta(months=1)

    for i in range(1, months + 1):
        if i == months:
            interest_part = remaining_balance * monthly_rate
            principal_part = remaining_balance
            annuity = principal_part + interest_part
        else:
            interest_part = remaining_balance * monthly_rate
            principal_part = annuity - interest_part

        interest_rounded = round(interest_part, 2)
        principal_rounded = round(principal_part, 2)
        annuity_rounded = round(interest_rounded + principal_rounded, 2)

        remaining_balance -= principal_rounded
        if remaining_balance < 0.01: remaining_balance = 0.0

        total_interest_paid += interest_rounded

        schedule.append({
            "nr": i,
            "date": current_date.strftime("%d.%m.%Y"),
            "payment": fmt_eur_it_with_cents(annuity_rounded),
            "interest": fmt_eur_it_with_cents(interest_rounded),
            "principal": fmt_eur_it_with_cents(principal_rounded),
            "balance": fmt_eur_it_with_cents(remaining_balance)
        })
        current_date += relativedelta(months=1)

    return round(annuity, 2), round(total_interest_paid, 2), schedule


def draw_border_and_pagenum(canv, doc):
    w, h = A4
    canv.saveState()
    m = 10 * mm;
    inner = 6
    canv.setStrokeColor(colors.HexColor("#0E2A47"));
    canv.setLineWidth(2)
    canv.rect(m, m, w - 2 * m, h - 2 * m, stroke=1, fill=0)
    canv.rect(m + inner, m + inner, w - 2 * (m + inner), h - 2 * (m + inner), stroke=1, fill=0)
    canv.setFont(F_MONO, 9);
    canv.setFillColor(colors.black)
    canv.drawCentredString(w / 2.0, 5 * mm, str(canv.getPageNumber()))
    canv.restoreState()


def img_box(path: str, max_h: float, max_w: float | None = None) -> Image | None:
    if not os.path.exists(path): return None
    try:
        ir = ImageReader(path);
        iw, ih = ir.getSize()
        scale_h = max_h / float(ih)
        scale_w = (max_w / float(iw)) if max_w else scale_h
        scale = min(scale_h, scale_w)
        return Image(path, width=iw * scale, height=ih * scale)
    except Exception:
        return None


# ---------- PDF BUILDERS ----------
def build_contract_pdf(values: dict) -> bytes:
    client = (values.get("client", "") or "").strip()
    amount = float(values.get("amount", 0) or 0)
    tan = float(values.get("tan", 0) or 0)
    eff = float(values.get("eff", 0) or 0)
    term = int(values.get("term", 0) or 0)
    bank_name = values.get("bank_name") or "ING Bank N.V. Milan Branch"
    service_fee = Decimal(str(values.get("service_fee_eur", "100.00")))

    monthly_payment_val, total_interest_val, schedule_list = calculate_amortization_schedule(amount, tan, term)
    total_debt_val = amount + total_interest_val

    context = {
        "client": client,
        "date_now": now_it_date(),
        "bank_name": bank_name,
        "company_legal": COMPANY["legal"],
        "company_addr": COMPANY["addr"],
        "company_reg": COMPANY["reg"],
        "company_contact": COMPANY["contact"],
        "company_email": COMPANY["email"],
        "company_web": COMPANY["web"],

        "amount": fmt_eur_it_with_cents(amount),
        "tan": f"{tan:.2f}".replace(".", ","),
        "eff": f"{eff:.2f}".replace(".", ","),
        "term": term,
        "monthly_payment": fmt_eur_it_with_cents(monthly_payment_val),
        "service_fee": fmt_eur_it_with_cents(float(service_fee)),
        "total_interest": fmt_eur_it_with_cents(total_interest_val),
        "total_debt": fmt_eur_it_with_cents(total_debt_val),
        "schedule": schedule_list,

        "logo_higobi": os.path.abspath(ASSETS["logo_higobi"]),
        "logo_partner1": os.path.abspath(ASSETS["logo_partner1"]),
        "logo_partner2": os.path.abspath(ASSETS["logo_partner2"]),
        "logo_santa": os.path.abspath(ASSETS["logo_santa"]),
        "sign_bank": os.path.abspath(ASSETS["sign_bank"]),
        "sign_c2g": os.path.abspath(ASSETS["sign_c2g"]),
    }

    env = Environment(loader=FileSystemLoader(str(BASE_DIR)))
    template = env.get_template('contract_template.html')
    rendered_html = template.render(context)
    return HTML(string=rendered_html, base_url=str(BASE_DIR)).write_pdf()


def bank_confirmation_build_pdf(values: dict) -> bytes:
    # Заменяем письмо-подтверждение (Bestaetigung_Kreditgenehmigung)
    client = (values.get("client", "") or "").strip()
    bank_name = values.get("bank_name") or "ING Bank N.V."

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm, topMargin=15 * mm,
                            bottomMargin=14 * mm)
    st = getSampleStyleSheet()
    st.add(ParagraphStyle(name="H", fontName=F_MONO_B, fontSize=13.4, leading=15.2, spaceAfter=4))
    st.add(ParagraphStyle(name="Mono", fontName=F_MONO, fontSize=10.6, leading=12.6))
    st.add(ParagraphStyle(name="MonoSm", fontName=F_MONO, fontSize=10.0, leading=11.6))
    st.add(ParagraphStyle(name="Key", fontName=F_MONO_B, fontSize=10.6, leading=12.6))

    story = []
    logo = img_box(ASSETS["logo_santa"], 26 * mm)
    if logo:
        logo.hAlign = "CENTER"
        story += [logo, Spacer(1, 4)]

    story.append(Paragraph(f"{bank_name} - Conferma Approvazione Credito", st["H"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Data: {now_it_date()} | Pratica N.: 2690497", st["MonoSm"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Gentile {client},", st["Mono"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Siamo lieti di confermare che la sua richiesta di finanziamento, presentata tramite il nostro "
        f"intermediario partner {COMPANY['legal']}, è stata formalmente approvata.", st["Mono"]
    ))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Per procedere all'erogazione finale dei fondi, è necessario completare il pagamento delle "
        "spese di istruttoria e di gestione della pratica del contratto.", st["Mono"]
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Cordiali Saluti,", st["Mono"]))
    story.append(Paragraph(bank_name, st["Key"]))

    doc.build(story, onFirstPage=draw_border_and_pagenum, onLaterPages=draw_border_and_pagenum)
    buf.seek(0)
    return buf.read()


def card_build_pdf(values: dict) -> bytes:
    # Обработка пустых полей с подстановкой линий для ручного заполнения
    name = (values.get("card_name", "") or "").strip() or "______________________________"
    addr = (values.get("card_addr", "") or "").strip() or "_______________________________________________________"

    case_num = "2690497"
    umr = f"ABAKO-{datetime.now().year}-2690497"

    date_it = now_it_date()
    bank_name = values.get("bank_name") or "ING Bank N.V."
    company_name = COMPANY.get("legal", "Intermediario")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H1", fontName=F_MONO_B, fontSize=14.2, leading=16.0, spaceAfter=6, alignment=1))
    styles.add(ParagraphStyle(name="H2", fontName=F_MONO_B, fontSize=12.2, leading=14.0, spaceBefore=6, spaceAfter=4))
    styles.add(ParagraphStyle(name="Mono", fontName=F_MONO, fontSize=10.6, leading=12.6))
    styles.add(ParagraphStyle(name="MonoS", fontName=F_MONO, fontSize=10.0, leading=11.8))
    styles.add(ParagraphStyle(name="Badge", fontName=F_MONO_B, fontSize=10.2, leading=12.0,
                              textColor=colors.HexColor("#0B5D1E"), alignment=1))

    story = []

    # Логотип
    logo = img_box(ASSETS.get("logo_partner1"), 26 * mm)
    if logo:
        logo.hAlign = "CENTER"
        story += [logo, Spacer(1, 4)]

    # Заголовок и метаданные
    story.append(Paragraph(f"{bank_name} – Erogazione su Carta", styles["H1"]))
    meta = Table([
        [Paragraph(f"Data: {date_it}", styles["MonoS"]), Paragraph(f"Pratica N.: {case_num}", styles["MonoS"])],
    ], colWidths=[doc.width / 2.0, doc.width / 2.0])
    meta.setStyle(TableStyle([
        ("ALIGN", (0, 0), (0, 0), "LEFT"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [meta]

    # Бейдж "Подтверждено"
    badge = Table([[Paragraph("CONFERMATO – Documento Operativo", styles["Badge"])]], colWidths=[doc.width])
    badge.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.9, colors.HexColor("#B9E8C8")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EFFEFA")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [badge, Spacer(1, 6)]

    # Вступительный текст (причина выпуска карты)
    intro = (
        "Al fine di garantire la disponibilità dei fondi in data odierna e a causa di tentativi di bonifico automatico "
        "non andati a buon fine, la Banca emetterà – in via eccezionale – una <b>carta di credito personalizzata</b>, "
        "con consegna <b>entro le ore 24:00</b> all'indirizzo indicato nel mandato SDD."
    )
    story.append(Paragraph(intro, styles["Mono"]))
    story.append(Spacer(1, 6))

    # Идентификационные данные
    story.append(Paragraph("Dati di Identificazione (da compilare)", styles["H2"]))
    story.append(Paragraph(f"• <b>Nome del Cliente:</b> {name}", styles["MonoS"]))
    story.append(Paragraph(f"• <b>Indirizzo di consegna (da SDD):</b> {addr}", styles["MonoS"]))
    story.append(Spacer(1, 6))

    # Что делать дальше
    story.append(Paragraph("Cosa fare ora", styles["H2"]))
    for line in [
        "1) Presenza all'indirizzo fino alle ore 24:00; tenere a portata di mano un documento d'identità.",
        "2) Consegna e firma alla ricezione della carta.",
        "3) Attivazione tramite OTP inviato ai recapiti del cliente.",
        "4) Fondi pre-accreditati – disponibili immediatamente dopo l'attivazione.",
        "5) Trasferimento sull'IBAN del cliente tramite bonifico bancario.",
    ]:
        story.append(Paragraph(line, styles["MonoS"]))
    story.append(Spacer(1, 6))

    # Условия
    story.append(Paragraph("Condizioni Operative", styles["H2"]))
    cond = [
        "• <b>Costo di emissione della carta:</b> 245 € (produzione + consegna express).",
        "• <b>Prime 5 disposizioni in uscita:</b> senza commissioni; successivamente secondo tariffario standard.",
        "• <b>Compensazione dei 245 €:</b> L'importo verrà detratto dalla prima rata; "
        "se la rata è < 245 €, il resto verrà compensato con le rate successive fino a totale saldo "
        "(l'adeguamento sarà visibile nel piano di ammortamento, senza aumento del costo totale del credito).",
        f"• <b>Flussi finanziari e coordinate:</b> sono gestiti da <b>{company_name}</b>; "
        f"le coordinate di pagamento (se necessarie) saranno fornite esclusivamente da {company_name}.",
    ]
    for p in cond:
        story.append(Paragraph(p, styles["MonoS"]))
    story.append(Spacer(1, 6))

    # Техническая таблица
    tech = Table([
        [Paragraph(f"Pratica: {case_num}", styles["MonoS"]), Paragraph(f"UMR: {umr}", styles["MonoS"])],
        [Paragraph(f"Indirizzo (SDD): {addr}", styles["MonoS"]), Paragraph("", styles["MonoS"])],
    ], colWidths=[doc.width * 0.62, doc.width * 0.38])
    tech.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [tech, Spacer(1, 6)]

    # Блок подписей
    story.append(Paragraph("Firme", styles["H2"]))
    sig_head_l = Paragraph("Firma Cliente", styles["MonoS"])
    sig_head_c = Paragraph("Firma Rappresentante<br/>Banca", styles["MonoS"])
    sig_head_r = Paragraph(f"Firma Rappresentante<br/>{company_name}", styles["MonoS"])

    sig_bank = img_box(ASSETS.get("sign_bank"), 22 * mm) if ASSETS.get("sign_bank") else None
    sig_c2g = img_box(ASSETS.get("sign_c2g"), 22 * mm) if ASSETS.get("sign_c2g") else None
    SIG_H = 24 * mm

    sig_tbl = Table(
        [
            [sig_head_l, sig_head_c, sig_head_r],
            ["", sig_bank or Spacer(1, SIG_H), sig_c2g or Spacer(1, SIG_H)],
            ["", "", ""],
        ],
        colWidths=[doc.width / 3.0, doc.width / 3.0, doc.width / 3.0],
        rowHeights=[9 * mm, SIG_H, 6 * mm],
        hAlign="CENTER",
    )
    sig_tbl.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 1), (-1, 1), "BOTTOM"),
        ("BOTTOMPADDING", (0, 1), (-1, 1), -6),
        ("LINEBELOW", (0, 2), (0, 2), 1.0, colors.black),
        ("LINEBELOW", (1, 2), (1, 2), 1.0, colors.black),
        ("LINEBELOW", (2, 2), (2, 2), 1.0, colors.black),
    ]))
    story.append(sig_tbl)

    try:
        doc.build(story, onFirstPage=draw_border_and_pagenum)
    except Exception as e:
        log.error(f"Ошибка при сборке PDF для карты: {e}")
        # Если сборка упала, возвращаем хотя бы пустой объект,
        # но лучше обработать ошибку

        # ВАЖНО: перемотка в начало
    buf.seek(0)
    pdf_content = buf.read()

    # Проверка: если вдруг PDF пустой, выводим лог
    if not pdf_content:
        log.error("Генерация PDF вернула 0 байт!")

    return pdf_content

def notary_build_pdf(user_data: dict) -> bytes:
    try:
        # Аккуратно достаем только саму сумму из словаря пользователя
        amount = user_data.get("notary_amount", "0")

        # Собираем данные для вставки в HTML-шаблон
        context = {
            "date_now": now_it_date(),
            "company_legal": COMPANY.get("legal", "ABAKO S.R.L."),
            "notary_amount": amount,  # <-- Теперь сюда пойдет чистое число (например, 160)
            "logo_ing": os.path.abspath(asset_path("ing_logo.png")),
            "stamp_ing": os.path.abspath(asset_path("ing_stamp.png")),
        }

        # Загружаем и рендерим HTML с помощью jinja2
        env = Environment(loader=FileSystemLoader(str(BASE_DIR)))
        template = env.get_template('notary_template.html')
        rendered_html = template.render(context)

        # Конвертируем готовый HTML в чистый PDF через weasyprint
        return HTML(string=rendered_html, base_url=str(BASE_DIR)).write_pdf()

    except Exception as e:
        log.error(f"Notary generation error: {e}")
        return b""

# ---------- BOT HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Добро пожаловать! Выберите одну из кнопок.", reply_markup=MAIN_KB)
    return ConversationHandler.END


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == BTN_CONTRACT:
        context.user_data["flow"] = "contract"
    elif txt == BTN_CARD:
        context.user_data["flow"] = "card"
    elif txt == BTN_NOTARY:
        context.user_data["flow"] = "notary"
        await update.message.reply_text("Укажите сумму для нотариуса (например, 250):")
        return ASK_NOTARY_AMOUNT

    # Automatically set country to Italy (IT)
    bp = get_bank_profile("IT")
    context.user_data["country"] = "IT"
    context.user_data["bank_name"] = bp["name"]
    context.user_data["bank_addr"] = bp["addr"]

    flow = context.user_data.get("flow")
    if flow == "contract":
        await update.message.reply_text("Имя клиента (например: Mario Rossi):")
        return ASK_CLIENT
    elif flow == "card":
        await update.message.reply_text("Выдача на карту: укажите ФИО клиента:")
        return CARD_NAME

    await update.message.reply_text("Неизвестный режим. Начните заново /start.")
    return ConversationHandler.END


# --- CONTRACT STEPS
async def ask_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Пожалуйста, укажите ФИО клиента.")
        return ASK_CLIENT
    context.user_data["client"] = name
    await update.message.reply_text("Сумма кредита (например, 5000):")
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["amount"] = parse_num(update.message.text)
        await update.message.reply_text("Процентная ставка TAN (например, 4.5):")
        return ASK_TAN
    except Exception:
        await update.message.reply_text("Неверный формат суммы. Введите число.")
        return ASK_AMOUNT


async def ask_tan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["tan"] = parse_num(update.message.text)
        await update.message.reply_text("Срок в месяцах (например, 48):")
        return ASK_TERM
    except Exception:
        await update.message.reply_text("Неверный формат. Введите число.")
        return ASK_TAN


async def ask_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["term"] = int(parse_num(update.message.text))
        await update.message.reply_text("Комиссия сервиса в евро (например, 170.00):")
        return ASK_FEE
    except Exception:
        await update.message.reply_text("Неверный формат. Введите целое число.")
        return ASK_TERM


async def ask_fee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["service_fee_eur"] = parse_num(update.message.text)
    except Exception:
        await update.message.reply_text("Неверный формат комиссии. Введите число.")
        return ASK_FEE

    # --- AUTOMATIC TAEG (EFF) CALCULATION ---
    amount = float(context.user_data.get("amount", 0))
    tan = float(context.user_data.get("tan", 0))
    term = int(context.user_data.get("term", 0))
    fee = float(context.user_data.get("service_fee_eur", 100))

    def calc_taeg(p, t_perc, n, f):
        if n <= 0 or p <= f:
            return t_perc
        r = (t_perc / 100.0) / 12.0
        pmt = p / n if r == 0 else p * (r / (1 - (1 + r) ** (-n)))
        actual_loan = p - f
        low, high, irr = 0.0, 1.0, 0.0
        for _ in range(100):
            mid = (low + high) / 2
            pv = pmt * n if mid == 0 else pmt * (1 - (1 + mid) ** (-n)) / mid
            if pv > actual_loan:
                low = mid
            else:
                high = mid
            irr = mid
        return (((1 + irr) ** 12) - 1) * 100.0

    context.user_data["eff"] = calc_taeg(amount, tan, term, fee)
    # ----------------------------------------

    # Генерируем Контракт
    pdf_bytes = build_contract_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bytes),
                           filename=f"Contratto_Preliminare_{now_it_date().replace('.', '')}.pdf"),
        caption="Готово. Контракт сформирован."
    )

    # Генерируем Письмо-подтверждение банка
    pdf_bank = bank_confirmation_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bank),
                           filename=f"Conferma_Approvazione_{now_it_date().replace('.', '')}.pdf"),
        caption="Готово. Письмо-подтверждение банка сформировано."
    )

    return ConversationHandler.END


# --- CARD FLOW ---
async def card_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = (update.message.text or "").strip()
    if not v:
        await update.message.reply_text("Укажите ФИО клиента.")
        return CARD_NAME
    context.user_data["card_name"] = v
    await update.message.reply_text("Адрес (улица/дом, CAP, город, провинция).")
    return CARD_ADDR


async def card_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = (update.message.text or "").strip()
    if not v:
        await update.message.reply_text("Укажите адрес полностью.")
        return CARD_ADDR
    context.user_data["card_addr"] = v

    card_bytes = card_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(card_bytes), filename=f"Erogazione_Carta_{now_it_date().replace('.', '')}.pdf"),
        caption="Готово. Документ о выдаче на карту сформирован."
    )
    return ConversationHandler.END


# --- NOTARY FLOW ---
async def notary_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notary_amount"] = update.message.text

    notary_bytes = notary_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(notary_bytes), filename=f"Notary_{now_it_date().replace('.', '')}.pdf"),
        caption="Готово. Нотариальный документ сформирован."
    )
    return ConversationHandler.END


# ---------- MAIN ----------
def main():
    token = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
    app = Application.builder().token(token).build()

    conv_contract = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_CONTRACT)), handle_menu)],
        states={
            ASK_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_client)],
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
            ASK_TAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_tan)],
            ASK_TERM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_term)],
            ASK_FEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_fee)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    conv_card = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_CARD)), handle_menu)],
        states={
            CARD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, card_name)],
            CARD_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, card_addr)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    conv_notary = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_NOTARY)), handle_menu)],
        states={
            ASK_NOTARY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, notary_amount)]
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv_contract)
    app.add_handler(conv_card)
    app.add_handler(conv_notary)
    app.add_handler(CommandHandler("start", start))

    app.run_polling()


if __name__ == "__main__":
    main()
