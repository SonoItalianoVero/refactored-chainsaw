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
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    Image, KeepTogether
)
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
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

SEPA = {"ci": "IT98ZZZ00123950001", "prenotice_days": 7}

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
    "exclam": asset_path("exclam.png", "exclam.PNG"),
    "notary_pdf": asset_path("notary_template.pdf"),
}

# ---------- UI ----------
BTN_AML = "Письмо АМЛ/комплаенс"
BTN_CARD = "Выдача на карту"
BTN_BOTH = "Контракт + SEPA"
BTN_NOTARY = "Редактировать нотариальное заверение (PDF)"

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_AML), KeyboardButton(BTN_CARD)],
        [KeyboardButton(BTN_BOTH), KeyboardButton(BTN_NOTARY)],
    ],
    resize_keyboard=True,
)

# ---------- STATES ----------
ASK_COUNTRY = 10
(ASK_CLIENT, ASK_AMOUNT, ASK_TAN, ASK_EFF, ASK_TERM) = range(20, 25)
ASK_FEE = 25
(SDD_NAME, SDD_ADDR, SDD_CITY, SDD_COUNTRY, SDD_ID, SDD_IBAN, SDD_BIC) = range(100, 107)
(AML_NAME, AML_ID, AML_IBAN) = range(200, 203)
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
# Помощник для отображения иконки восклицательного знака
def exclam_flowable(size):
    path = ASSETS.get("exclam")
    if not path or not os.path.exists(path):
        return None
    return img_box(path, size)

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
    service_fee = Decimal(str(values.get("service_fee_eur", "170.00")))

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
        "Per procedere all'erogazione finale dei fondi, è necessario completare il pagamento della commissione "
        "relativa ai servizi di intermediazione e validazione legale del contratto.", st["Mono"]
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Cordiali Saluti,", st["Mono"]))
    story.append(Paragraph(bank_name, st["Key"]))

    doc.build(story, onFirstPage=draw_border_and_pagenum, onLaterPages=draw_border_and_pagenum)
    buf.seek(0)
    return buf.read()


class Typesetter:
    def __init__(self, canv, left=18 * mm, top=None, line_h=14.2):
        self.c = canv;
        self.left = left;
        self.x = left
        self.y = top if top is not None else A4[1] - 18 * mm
        self.line_h = line_h;
        self.font_r = F_MONO;
        self.font_b = F_MONO_B
        self.size = 11

    def _w(self, s, bold=False, size=None):
        size = size or self.size
        return pdfmetrics.stringWidth(s, self.font_b if bold else self.font_r, size)

    def nl(self, n=1):
        self.x = self.left;
        self.y -= self.line_h * n

    def seg(self, t, bold=False, size=None):
        size = size or self.size
        self.c.setFont(self.font_b if bold else self.font_r, size)
        self.c.drawString(self.x, self.y, t);
        self.x += self._w(t, bold, size)

    def line(self, t="", bold=False, size=None):
        self.seg(t, bold, size);
        self.nl()

    def para(self, text, bold=False, size=None, indent=0, max_w=None):
        size = size or self.size
        max_w = max_w or (A4[0] - self.left * 2)
        words = text.split();
        line = "";
        first = True
        while words:
            w = words[0];
            trial = (line + " " + w).strip()
            if self._w(trial, bold, size) <= max_w - (indent if first else 0):
                line = trial;
                words.pop(0)
            else:
                self.c.setFont(self.font_b if bold else self.font_r, size)
                x0 = self.left + (indent if first else 0)
                self.c.drawString(x0, self.y, line)
                self.y -= self.line_h;
                first = False;
                line = ""
        if line:
            self.c.setFont(self.font_b if bold else self.font_r, size)
            x0 = self.left + (indent if first else 0)
            self.c.drawString(x0, self.y, line);
            self.y -= self.line_h

    def kv(self, label, value, size=None, max_w=None):
        size = size or self.size
        max_w = max_w or (A4[0] - self.left * 2)
        label_txt = f"{label}: ";
        lw = self._w(label_txt, True, size)
        self.c.setFont(self.font_b, size);
        self.c.drawString(self.left, self.y, label_txt)
        rem_w = max_w - lw;
        old_left = self.left;
        self.left += lw
        self.para(value, bold=False, size=size, indent=0, max_w=rem_w)
        self.left = old_left


def sepa_build_pdf(values: dict) -> bytes:
    name = (values.get("name", "") or "").strip() or "______________________________"
    addr = (values.get("addr", "") or "").strip() or "_______________________________________________________"
    capcity = (values.get("capcity", "") or "").strip() or "__________________________________________"
    country = (values.get("country", "") or "").strip() or "____________________"
    idnum = (values.get("idnum", "") or "").strip() or "________________"
    iban = ((values.get("iban", "") or "").replace(" ", "")) or "__________________________________"
    bic = (values.get("bic", "") or "").strip() or "___________"

    date_it = now_it_date()
    umr = f"ABAKO-{datetime.now().year}-2690497"
    bank_name = values.get("bank_name") or "ING Bank N.V."
    bank_addr = values.get("bank_addr") or ""

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    ts = Typesetter(c, left=18 * mm, top=A4[1] - 22 * mm, line_h=14.2)

    ts.line("Mandato di addebito diretto SEPA (SDD)", bold=True)
    ts.seg("Schema: ", True);
    ts.seg("Y CORE   X B2B   ")
    ts.seg("Tipo di pagamento: ", True);
    ts.line("Y Ricorrente   X Singolo")

    ts.kv("Identificativo del Creditore (CI)", SEPA["ci"])
    ts.kv("Riferimento del Mandato (UMR)", umr)
    ts.nl()

    ts.line("Dati del Pagatore (Intestatario del conto)", bold=True)
    ts.kv("Nome/Azienda", name)
    ts.kv("Indirizzo", addr)
    ts.kv("CAP / Città / Provincia", capcity)
    ts.kv("Paese", country + "    Codice Fiscale/P.IVA: " + idnum)
    ts.kv("IBAN (senza spazi)", iban)
    ts.kv("BIC", bic)
    ts.nl()

    ts.line("Autorizzazione", bold=True)
    ts.para(
        f"Con la mia firma autorizzo (A) {bank_name} a inviare disposizioni di addebito alla mia banca e (B) la mia banca ad addebitare il mio conto in base alle istruzioni del creditore.")
    ts.para(
        "Per lo schema CORE, ho il diritto di richiedere alla mia banca il rimborso entro 8 settimane dalla data di addebito.")
    ts.kv("Pre-Notifica", f"{SEPA['prenotice_days']} giorni prima della scadenza")
    ts.kv("Data", date_it)
    ts.para("Firma del pagatore: non richiesta; i documenti sono preparati dall'intermediario.")
    ts.nl()

    ts.line("Dati del Creditore", bold=True)
    ts.kv("Denominazione", bank_name)
    ts.kv("Indirizzo", bank_addr)
    ts.kv("SEPA CI", SEPA["ci"])
    ts.nl()

    ts.line("Incaricato alla raccolta del mandato (Intermediario)", bold=True)
    ts.kv("Nome", COMPANY["legal"])
    ts.kv("Indirizzo", COMPANY["addr"])
    ts.kv("Contatto", f"{COMPANY['contact']} | E-Mail: {COMPANY['email']}")
    ts.nl()

    ts.line("Clausole Opzionali", bold=True)
    ts.para("[Y] Autorizzo la conservazione elettronica di questo mandato.")
    ts.para("[Y] In caso di variazione dell'IBAN o dei dati mi impegno a comunicarlo per iscritto.")

    c.showPage();
    c.save();
    buf.seek(0)
    return buf.read()


def aml_build_pdf(values: dict) -> bytes:
    # Исходные данные
    name = (values.get("aml_name", "") or "").strip() or "[_____________________________]"
    idn  = (values.get("aml_id", "") or "").strip() or "[________________]"
    iban = ((values.get("aml_iban", "") or "").replace(" ", "")) or "[_____________________________]"
    date_it = now_it_date()

    VORGANG_NR = "2690497"
    PAY_DEADLINE = 7
    PAY_AMOUNT = Decimal("285.00")

    bank_name = values.get("bank_name") or "ING Bank N.V."
    bank_addr = values.get("bank_addr") or ""
    BANK_DEPT = "Dipartimento Sicurezza & Anti-Frode"
    company_name = COMPANY.get("legal", "Intermediario")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=17*mm, rightMargin=17*mm,
        topMargin=14*mm, bottomMargin=14*mm
    )

    # Определение стилей (как в немецкой версии)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H",      fontName=F_MONO_B, fontSize=13.4, leading=15.2, spaceAfter=4))
    styles.add(ParagraphStyle(name="Hsub",   fontName=F_MONO,   fontSize=10.2, leading=12.0, textColor=colors.HexColor("#334")))
    styles.add(ParagraphStyle(name="H2",     fontName=F_MONO_B, fontSize=12.2, leading=14.0, spaceBefore=5, spaceAfter=3))
    styles.add(ParagraphStyle(name="Mono",   fontName=F_MONO,   fontSize=10.6, leading=12.6))
    styles.add(ParagraphStyle(name="MonoSm", fontName=F_MONO,   fontSize=10.0, leading=11.8))
    styles.add(ParagraphStyle(name="Key",    fontName=F_MONO_B, fontSize=10.6, leading=12.6))
    styles.add(ParagraphStyle(name="Box",    fontName=F_MONO,   fontSize=10.2, leading=12.0))

    # --- СТРАНИЦА 1 ---
    page1 = []
    logo = img_box(ASSETS["logo_partner1"], 26*mm)
    if logo:
        logo.hAlign = "CENTER"
        page1 += [logo, Spacer(1, 6)]

    page1.append(Paragraph(f"{bank_name} – Richiesta di Pagamento", styles["H"]))
    page1.append(Paragraph(BANK_DEPT, styles["Hsub"]))
    page1.append(Paragraph(f"Pratica N.: {VORGANG_NR}", styles["MonoSm"]))
    page1.append(Paragraph(f"Data: {date_it}", styles["MonoSm"]))
    page1.append(Spacer(1, 5))

    # Желтый блок предупреждения (Preamble)
    warn_icon_l = exclam_flowable(10 * mm)
    warn_icon_r = exclam_flowable(10 * mm)
    preamble_text = (
        "A seguito di un riesame interno (le cui procedure e metodologie sono riservate), "
        "il prestatore ha associato il Suo profilo a una maggiore probabilità di ritardo o insolvenza "
        "nei pagamenti. Per la gestione del rischio e la prosecuzione del processo di erogazione, "
        f"è richiesto un <b>Pagamento di Garanzia / Premio Assicurativo di {fmt_eur(PAY_AMOUNT)}</b>, "
        f"da corrispondere <b>entro {PAY_DEADLINE} giorni lavorativi</b>."
    )
    pre_tbl = Table(
        [[warn_icon_l or "", Paragraph(preamble_text, styles["MonoSm"]), warn_icon_r or ""]],
        colWidths=[12*mm, doc.width - 24*mm, 12*mm]
    )
    pre_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("ALIGN", (2, 0), (2, 0), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#E0A800")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7E6")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),  ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    page1 += [pre_tbl, Spacer(1, 6)]

    # Данные посредника
    page1.append(Paragraph(f"<b>Destinatario (Intermediario):</b> {COMPANY['legal']}", styles["Mono"]))
    page1.append(Paragraph(COMPANY["addr"], styles["MonoSm"]))
    page1.append(Paragraph(f"Contatto: {COMPANY['contact']} | E-Mail: {COMPANY['email']} | Web: {COMPANY['web']}",
                           styles["MonoSm"]))
    page1.append(Spacer(1, 5))

    page1.append(Paragraph(
        "In merito alla verifica interna supplementare relativa alla pratica sopra menzionata, comunichiamo quanto segue.",
        styles["Mono"]
    ))
    page1.append(Spacer(1, 5))

    # Данные клиента
    page1.append(Paragraph("Dati del Richiedente (per identificazione)", styles["H2"]))
    for line in [
        f"• <b>Nome e Cognome:</b> {name}",
        f"• <b>Documento / P.IVA:</b> {idn}",
        f"• <b>IBAN del Cliente:</b> {iban}",
    ]:
        page1.append(Paragraph(line, styles["MonoSm"]))
    page1.append(Spacer(1, 5))

    # 1) Pagamento richiesto
    page1.append(Paragraph("1) Pagamento richiesto", styles["H2"]))
    for b in [
        "• <b>Tipologia:</b> Pagamento di Garanzia / Premio Assicurativo",
        f"• <b>Importo:</b> {fmt_eur(PAY_AMOUNT)}",
        f"• <b>Termine di esecuzione:</b> entro {PAY_DEADLINE} giorni lavorativi dal ricevimento della presente",
        f"• <b>Modalità di esecuzione:</b> le coordinate di pagamento saranno comunicate al cliente direttamente dal "
        f"manager incaricato di {company_name} (nessun pagamento a terzi).",
        "• <b>Soggetto tenuto al pagamento:</b> il richiedente (Cliente)",
    ]:
        page1.append(Paragraph(b, styles["MonoSm"]))
    page1.append(Spacer(1, 5))

    # 2) Natura della richiesta
    page1.append(Paragraph("2) Natura della richiesta", styles["H2"]))
    page1.append(Paragraph(
        "La presente richiesta è obbligatoria, preventiva e non negoziabile. "
        "Il pagamento in oggetto costituisce un prerequisito essenziale per il proseguimento del processo di erogazione.",
        styles["MonoSm"]
    ))
    page1.append(Spacer(1, 5))

    # 3) Obblighi dell'intermediario
    page1.append(Paragraph("3) Obblighi dell'Intermediario", styles["H2"]))
    for b in [
        "• Informare il richiedente in merito alla presente comunicazione e ottenere un riscontro.",
        "• Fornire le coordinate di pagamento e gestire l'incasso/inoltro secondo le istruzioni della banca.",
        "• Trasmettere la prova di pagamento (copia bonifico/ricevuta) alla banca e verificare la congruenza "
        "con i dati del cliente (Nome e Cognome ↔ IBAN).",
        "• Gestire le comunicazioni con la banca in nome e per conto del cliente.",
    ]:
        page1.append(Paragraph(b, styles["MonoSm"]))

    # --- СТРАНИЦА 2 ---
    page2 = []
    page2.append(Spacer(1, 6))
    page2.append(Paragraph("4) Conseguenze in caso di mancato pagamento", styles["H2"]))
    page2.append(Paragraph(
        "In caso di mancato pagamento entro il termine stabilito, la banca rifiuterà unilateralmente l'erogazione "
        "e chiuderà la pratica, revocando ogni valutazione o conferma preliminare e annullando le "
        "relative condizioni economiche accordate.",
        styles["MonoSm"]
    ))
    page2.append(Spacer(1, 6))

    # Синий блок с инструкциями
    info = (f"Le coordinate di pagamento saranno fornite al cliente direttamente dal manager incaricato di "
            f"{company_name}. Si prega di non effettuare pagamenti a terzi o su conti diversi da quelli indicati.")
    info_box = Table([[Paragraph(info, styles["Box"])]], colWidths=[doc.width])
    info_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#96A6C8")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EEF3FF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),  ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    page2.append(info_box)
    page2.append(Spacer(1, 8))

    # Футер банка
    page2.append(Paragraph(bank_name, styles["Key"]))
    page2.append(Paragraph(BANK_DEPT, styles["MonoSm"]))
    page2.append(Paragraph(f"Indirizzo: {bank_addr}", styles["MonoSm"]))

    # Сборка документа
    story = []
    story.extend(page1)
    story.append(PageBreak())
    story.extend(page2)

    doc.build(story, onFirstPage=draw_border_and_pagenum, onLaterPages=draw_border_and_pagenum)
    buf.seek(0)
    return buf.read()


def card_build_pdf(values: dict) -> bytes:
    # Обработка пустых полей с подстановкой линий для ручного заполнения
    name = (values.get("card_name", "") or "").strip() or "______________________________"
    addr = (values.get("card_addr", "") or "").strip() or "_______________________________________________________"

    case_num = "2690497"
    umr = f"GAFNER-{datetime.now().year}-2690497"

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
        "• <b>Costo di emissione della carta:</b> 290 € (produzione + consegna express).",
        "• <b>Prime 5 disposizioni in uscita:</b> senza commissioni; successivamente secondo tariffario standard.",
        "• <b>Compensazione dei 290 €:</b> L'importo verrà detratto dalla prima rata; "
        "se la rata è < 290 €, il resto verrà compensato con le rate successive fino a totale saldo "
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
def _parse_country(txt: str) -> str | None:
    s = (txt or "").strip().lower()
    if s in ("it", "италия", "italy", "italia"): return "IT"
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Добро пожаловать! Выберите одну из кнопок.", reply_markup=MAIN_KB)
    return ConversationHandler.END


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == BTN_BOTH:
        context.user_data["flow"] = "both"
    elif txt == BTN_AML:
        context.user_data["flow"] = "aml"
    elif txt == BTN_CARD:
        context.user_data["flow"] = "card"
    elif txt == BTN_NOTARY:
        context.user_data["flow"] = "notary"
        await update.message.reply_text("Укажите сумму для нотариуса (например, 250):")
        return ASK_NOTARY_AMOUNT

    await update.message.reply_text("Пожалуйста, укажите: Италия (IT).")
    return ASK_COUNTRY


async def ask_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cc = _parse_country(update.message.text)
    if not cc:
        await update.message.reply_text("Пожалуйста, укажите: Италия (IT).")
        return ASK_COUNTRY

    bp = get_bank_profile(cc)
    context.user_data["country"] = cc
    context.user_data["bank_name"] = bp["name"]
    context.user_data["bank_addr"] = bp["addr"]

    flow = context.user_data.get("flow")
    if flow == "both":
        await update.message.reply_text("Имя клиента (например: Mario Rossi)")
        return ASK_CLIENT
    elif flow == "aml":
        await update.message.reply_text("АМЛ-письмо: укажите ФИО.")
        return AML_NAME
    elif flow == "card":
        await update.message.reply_text("Выдача на карту: укажите ФИО клиента.")
        return CARD_NAME

    await update.message.reply_text("Неизвестный режим. Начните заново /start.")
    return ConversationHandler.END


# --- CONTRACT STEPS (используются и для BOTH)
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
        await update.message.reply_text("Эффективная ставка EFF (например, 4.8):")
        return ASK_EFF
    except Exception:
        await update.message.reply_text("Неверный формат. Введите число.")
        return ASK_TAN


async def ask_eff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["eff"] = parse_num(update.message.text)
        await update.message.reply_text("Срок в месяцах (например, 48):")
        return ASK_TERM
    except Exception:
        await update.message.reply_text("Неверный формат. Введите число.")
        return ASK_EFF


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

    # Переходим к SEPA
    if context.user_data.get("flow") == "both":
        context.user_data["name"] = context.user_data.get("client", "")
        await update.message.reply_text("Теперь данные для SEPA-мандата.\nУкажите адрес (улица/дом).")
        return SDD_ADDR

    return ConversationHandler.END


# --- SDD STEPS
async def sdd_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = (update.message.text or "").strip()
    if not v:
        await update.message.reply_text("Укажите ФИО/название.")
        return SDD_NAME
    context.user_data["name"] = v
    await update.message.reply_text("Адрес (улица/дом)")
    return SDD_ADDR


async def sdd_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = (update.message.text or "").strip()
    if not v:
        await update.message.reply_text("Укажите адрес.")
        return SDD_ADDR
    context.user_data["addr"] = v
    await update.message.reply_text("CAP / Città / Provincia (в одну строку).")
    return SDD_CITY


async def sdd_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = (update.message.text or "").strip()
    if not v:
        await update.message.reply_text("Укажите город.")
        return SDD_CITY
    context.user_data["capcity"] = v
    await update.message.reply_text("Страна:")
    return SDD_COUNTRY


async def sdd_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["country"] = (update.message.text or "").strip()
    await update.message.reply_text("Codice Fiscale или P.IVA:")
    return SDD_ID


async def sdd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["idnum"] = (update.message.text or "").strip()
    await update.message.reply_text("IBAN (без пробелов):")
    return SDD_IBAN


async def sdd_iban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["iban"] = (update.message.text or "").strip()
    await update.message.reply_text("BIC банка:")
    return SDD_BIC


async def sdd_bic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bic"] = (update.message.text or "").strip()

    sepa_bytes = sepa_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(sepa_bytes), filename=f"SEPA_{now_it_date().replace('.', '')}.pdf"),
        caption="Готово. SEPA мандат сформирован."
    )
    return ConversationHandler.END


# --- AML FLOW ---
async def aml_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["aml_name"] = update.message.text
    await update.message.reply_text("Укажите ID (Codice Fiscale / P.IVA):")
    return AML_ID


async def aml_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["aml_id"] = update.message.text
    await update.message.reply_text("Укажите IBAN:")
    return AML_IBAN


async def aml_iban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["aml_iban"] = update.message.text

    aml_bytes = aml_build_pdf(context.user_data)
    await update.message.reply_document(
        document=InputFile(io.BytesIO(aml_bytes), filename=f"AML_{now_it_date().replace('.', '')}.pdf"),
        caption="Готово. AML письмо сформировано."
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

    conv_both = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_BOTH)), handle_menu)],
        states={
            ASK_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_country)],
            ASK_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_client)],
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
            ASK_TAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_tan)],
            ASK_EFF: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_eff)],
            ASK_TERM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_term)],
            ASK_FEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_fee)],
            SDD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_name)],
            SDD_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_addr)],
            SDD_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_city)],
            SDD_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_country)],
            SDD_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_id)],
            SDD_IBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_iban)],
            SDD_BIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, sdd_bic)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    conv_aml = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_AML)), handle_menu)],
        states={
            ASK_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_country)],
            AML_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, aml_name)],
            AML_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, aml_id)],
            AML_IBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, aml_iban)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    conv_card = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(re.escape(BTN_CARD)), handle_menu)],
        states={
            ASK_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_country)],
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

    app.add_handler(conv_both)
    app.add_handler(conv_aml)
    app.add_handler(conv_card)
    app.add_handler(conv_notary)
    app.add_handler(CommandHandler("start", start))

    app.run_polling()


if __name__ == "__main__":
    main()