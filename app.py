import os
import sys
import json
import uuid
import sqlite3
import logging
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, abort

# Garante que pacotes instalados localmente são encontrados
_local_pkgs = os.path.join(os.path.dirname(__file__), "vendor")
if _local_pkgs not in sys.path:
    sys.path.insert(0, _local_pkgs)

import smtplib
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import anthropic
import mercadopago
from fpdf import FPDF

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-mude-em-producao")

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
MERCADOPAGO_TOKEN   = os.environ.get("MERCADOPAGO_ACCESS_TOKEN", "")
GMAIL_USER          = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
FROM_EMAIL          = os.environ.get("FROM_EMAIL", GMAIL_USER)
APPS_SCRIPT_WEBHOOK_URL = os.environ.get("APPS_SCRIPT_WEBHOOK_URL", "")
PRODUCT_PRICE       = float(os.environ.get("PRODUCT_PRICE", "97"))
BASE_URL            = os.environ.get("BASE_URL", "http://localhost:5000")
ADMIN_PASSWORD      = os.environ.get("ADMIN_PASSWORD", "admin123")

DB_PATH     = "diagnostico.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # Railway PostgreSQL

# ---------------------------------------------------------------------------
# DATABASE — suporta PostgreSQL (produção) e SQLite (local/fallback)
# ---------------------------------------------------------------------------
class _DbConn:
    """Wrapper que normaliza diferenças entre sqlite3 e psycopg2."""
    def __init__(self):
        if DATABASE_URL:
            import psycopg2, psycopg2.extras
            self._conn = psycopg2.connect(DATABASE_URL)
            self._pg   = True
            self._rf   = psycopg2.extras.RealDictCursor
        else:
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.row_factory = sqlite3.Row
            self._pg   = False
            self._rf   = None

    def execute(self, sql, params=()):
        if self._pg:
            sql = sql.replace("?", "%s")
            cur = self._conn.cursor(cursor_factory=self._rf)
        else:
            cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        try: self._conn.close()
        except Exception: pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None: self._conn.commit()
            else:                self._conn.rollback()
        finally:
            self.close()


def get_db():
    return _DbConn()


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                name TEXT,
                payment_id TEXT,
                status TEXT DEFAULT 'pending',
                payment_source TEXT DEFAULT 'manual',
                form_token TEXT UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS diagnostics (
                id TEXT PRIMARY KEY,
                purchase_id TEXT NOT NULL,
                form_data TEXT,
                report_text TEXT,
                pdf_path TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(purchase_id) REFERENCES purchases(id)
            )
        """)
        db.commit()
        # Adiciona coluna payment_source se nao existir (migracao)
        try:
            db.execute("ALTER TABLE purchases ADD COLUMN payment_source TEXT DEFAULT 'manual'")
            db.commit()
        except Exception:
            pass  # coluna ja existe

init_db()

# ---------------------------------------------------------------------------
# PAGAMENTO (Mercado Pago)
# ---------------------------------------------------------------------------
@app.route("/criar-pagamento", methods=["POST"])
def criar_pagamento():
    email = request.form.get("email", "").strip().lower()
    name  = request.form.get("name", "").strip()

    if not email or "@" not in email:
        return render_template("index.html", error="Email inválido. Tente novamente.")

    purchase_id = str(uuid.uuid4())

    with get_db() as db:
        db.execute(
            "INSERT INTO purchases (id, email, name, status) VALUES (?,?,?,'pending')",
            (purchase_id, email, name)
        )
        db.commit()

    sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)

    preference_data = {
        "items": [{
            "title": "Diagnóstico de Gestão Rural - Relatório Personalizado",
            "quantity": 1,
            "unit_price": PRODUCT_PRICE,
            "currency_id": "BRL",
        }],
        "payer": {"email": email, "name": name},
        "external_reference": purchase_id,
        "back_urls": {
            "success": f"{BASE_URL}/diagnostico/{purchase_id}",
            "failure": f"{BASE_URL}/pagamento-falhou",
            "pending": f"{BASE_URL}/pagamento-pendente",
        },
        "auto_return": "approved",
        "notification_url": f"{BASE_URL}/webhook/mercadopago",
        "statement_descriptor": "AGRO DIAGNOSTICO",
        "payment_methods": {
            "excluded_payment_types": [],
            "installments": 1
        }
    }

    result = sdk.preference().create(preference_data)

    if result["status"] == 201:
        init_point = result["response"]["init_point"]
        return redirect(init_point)
    else:
        log.error("Erro Mercado Pago: %s", result)
        return render_template("index.html", error="Erro ao criar pagamento. Tente novamente.")


@app.route("/webhook/mercadopago", methods=["POST"])
def webhook_mercadopago():
    """Webhook do Mercado Pago - confirma pagamento e envia o formulário por email."""
    try:
        data = request.get_json(force=True) or {}
        topic = data.get("type") or request.args.get("topic", "")

        if topic not in ("payment", "merchant_order"):
            return jsonify({"ok": True}), 200

        payment_id = None
        if topic == "payment":
            payment_id = str(data.get("data", {}).get("id", "") or request.args.get("id", ""))

        if not payment_id:
            return jsonify({"ok": True}), 200

        sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)
        payment_info = sdk.payment().get(payment_id)

        if payment_info["status"] != 200:
            return jsonify({"ok": True}), 200

        payment = payment_info["response"]
        if payment.get("status") != "approved":
            return jsonify({"ok": True}), 200

        purchase_id = payment.get("external_reference", "")
        if not purchase_id:
            return jsonify({"ok": True}), 200

        with get_db() as db:
            row = db.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
            if not row or row["status"] == "approved":
                return jsonify({"ok": True}), 200

            form_token = str(uuid.uuid4())
            db.execute(
                "UPDATE purchases SET status='approved', payment_id=?, form_token=?, payment_source='mercadopago' WHERE id=?",
                (payment_id, form_token, purchase_id)
            )
            db.commit()

        _enviar_email_formulario(row["email"], row["name"], purchase_id, form_token)
        return jsonify({"ok": True}), 200

    except Exception as e:
        log.exception("Erro no webhook: %s", e)
        return jsonify({"ok": True}), 200


# ---------------------------------------------------------------------------
# FORMULÁRIO DE DIAGNÓSTICO
# ---------------------------------------------------------------------------
@app.route("/diagnostico/<purchase_id>")
def diagnostico_get(purchase_id):
    """Página do formulário após pagamento aprovado."""
    with get_db() as db:
        row = db.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()

    if not row:
        abort(404)

    # Garante que o pagamento foi aprovado (ou aprova se vier do back_url de sucesso)
    if row["status"] == "pending":
        # Tenta verificar o pagamento manualmente via Mercado Pago
        _verificar_pagamento_manual(purchase_id)
        with get_db() as db:
            row = db.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()

    if row["status"] not in ("approved",):
        return render_template("aguardando.html", purchase_id=purchase_id)

    # Verifica se já enviou diagnóstico
    with get_db() as db:
        diag = db.execute("SELECT * FROM diagnostics WHERE purchase_id=?", (purchase_id,)).fetchone()
    if diag and diag["status"] == "done":
        return render_template("ja_enviado.html", email=row["email"])

    return render_template("form.html", purchase_id=purchase_id, name=row["name"], email=row["email"])


@app.route("/diagnostico/<purchase_id>", methods=["POST"])
def diagnostico_post(purchase_id):
    """Recebe o formulário, gera o relatório e envia por email."""
    with get_db() as db:
        row = db.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()

    if not row or row["status"] not in ("approved",):
        abort(403)

    form_data = {k: v for k, v in request.form.items()}
    form_data["_name"]  = row["name"]
    form_data["_email"] = row["email"]

    diag_id = str(uuid.uuid4())
    with get_db() as db:
        db.execute(
            "INSERT INTO diagnostics (id, purchase_id, form_data, status) VALUES (?,?,?,'processing')",
            (diag_id, purchase_id, json.dumps(form_data, ensure_ascii=False))
        )
        db.commit()

    # Gera em background para não travar o request (evita timeout Railway)
    import threading
    _name  = row["name"]
    _email = row["email"]

    def _gerar_async():
        try:
            log.info("Gerando relatorio para %s ...", _email)
            report_text = _gerar_relatorio_claude(form_data)
            pdf_path    = _gerar_pdf(diag_id, _name, report_text, form_data)
            # Marca done apos PDF gerado — falha de email nao deve reverter
            with get_db() as db:
                db.execute(
                    "UPDATE diagnostics SET report_text=?, pdf_path=?, status='done' WHERE id=?",
                    (report_text, pdf_path, diag_id)
                )
                db.commit()
            log.info("Relatorio gerado para %s", _email)
        except Exception as e:
            log.exception("Erro ao gerar relatorio: %s", e)
            with get_db() as db:
                db.execute("UPDATE diagnostics SET status='error' WHERE id=?", (diag_id,))
                db.commit()
            return
        # Email separado — falha nao-fatal
        try:
            _enviar_relatorio(_email, _name, pdf_path)
            log.info("Relatorio enviado para %s", _email)
        except Exception as e:
            log.warning("Falha ao enviar email para %s: %s", _email, e)

    threading.Thread(target=_gerar_async, daemon=True).start()
    return render_template("obrigado.html", email=row["email"])


# ---------------------------------------------------------------------------
# CLAUDE API — GERAÇÃO DO RELATÓRIO
# ---------------------------------------------------------------------------
def _gerar_relatorio_claude(fd: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Você é um consultor sênior de gestão rural com MBA e experiência em agronegócio.
Analise as respostas abaixo de um produtor/gestor rural e produza um DIAGNÓSTICO DE GESTÃO PROFISSIONAL completo.

DADOS DO CLIENTE:
- Nome: {fd.get('_name', 'N/A')}
- Tipo de atividade: {fd.get('atividade', 'N/A')}
- Área total: {fd.get('area_hectares', 'N/A')} hectares ({fd.get('area_propria', 'N/A')} próprios, {fd.get('area_arrendada', 'N/A')} arrendados)
- Faturamento anual estimado: {fd.get('faturamento', 'N/A')}
- Funcionários fixos: {fd.get('funcionarios', 'N/A')}
- Software de gestão utilizado: {fd.get('software_gestao', 'N/A')}
- Como controla custos hoje: {fd.get('controle_custos', 'N/A')}
- Calcula custo por hectare: {fd.get('calcula_custo_ha', 'N/A')} - Frequência: {fd.get('frequencia_calculo', 'N/A')}
- Como decide compra de insumos: {fd.get('decisao_insumos', 'N/A')}
- Acesso a crédito rural / negocia com dados: {fd.get('credito_rural', 'N/A')}
- Maiores desafios de gestão (em suas próprias palavras): {fd.get('desafios', 'N/A')}
- Áreas que precisam de mais atenção: {fd.get('areas_atencao', 'N/A')}
- Meta de crescimento para 2 anos: {fd.get('meta_crescimento', 'N/A')}
- Informações adicionais: {fd.get('info_adicional', 'N/A')}

ESTRUTURA DO RELATÓRIO (siga exatamente esta estrutura, usando markdown com ## para títulos de seção e ### para subtítulos):

## RESUMO EXECUTIVO
(Síntese de 250-300 palavras: situação atual, principais pontos de atenção, potencial identificado e prioridade de ação. Tom direto e objetivo.)

## PERFIL DA OPERAÇÃO
(Caracterização detalhada da propriedade, escala, modelo de negócio, posicionamento competitivo no contexto regional.)

## DIAGNÓSTICO SITUACIONAL

### Gestão Financeira e de Custos
(Avalie profundamente: controle de custos, conhecimento da margem real, uso de dados financeiros para decisão, gestão do fluxo de caixa. Seja específico sobre o que está bem e o que precisa melhorar.)

### Gestão Operacional e Produtiva
(Avalie: processos de produção, eficiência operacional, logística, gestão de insumos, planejamento de safra.)

### Gestão de Pessoas e Liderança
(Avalie: estrutura de equipe, processos de seleção e treinamento, cultura de gestão, delegação vs. centralização.)

### Adoção de Tecnologia e Inovação
(Avalie: uso de ferramentas digitais, dado como ativo estratégico, abertura à inovação, benchmarking tecnológico.)

### Gestão Comercial e de Mercado
(Avalie: estratégia de venda, hedge e gestão de risco de preço, relacionamento com compradores, diversificação de receita.)

## PONTOS CRÍTICOS E RISCOS

(Liste e explique 3-5 pontos críticos concretos que representam risco real para a operação nos próximos 12 meses. Seja específico, sem jargão vazio.)

## PLANO DE AÇÃO PRIORITÁRIO

### Ações Imediatas (próximos 30 dias)
(2-3 ações concretas, com o QUÊ fazer, POR QUÊ é urgente e COMO executar.)

### Ações de Médio Prazo (2-6 meses)
(3-4 ações estruturantes com objetivo claro e indicador de sucesso.)

### Ações Estratégicas (6-24 meses)
(2-3 iniciativas de transformação de médio prazo.)

## CONCLUSÃO E PRÓXIMOS PASSOS
(Fechamento direto: o que define o sucesso da operação nos próximos 2 anos, e qual é o primeiro passo concreto a ser dado esta semana.)

---
INSTRUÇÕES DE ESTILO:
- Escreva com profissionalismo mas sem pedantismo. Tom direto, como um bom consultor que respeita o tempo do cliente.
- Seja ESPECÍFICO: use os dados fornecidos, não responda genericamente.
- Cada seção deve ter pelo menos 200 palavras.
- Não use jargão vazio tipo "é fundamental", "é de suma importância" etc.
- Quando identificar problema, explique o custo concreto de não resolver.
- Total esperado: 2.000 a 2.500 palavras.
"""

    import time
    for attempt in range(4):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )
            return message.content[0].text
        except Exception as exc:
            if attempt < 3 and getattr(exc, 'status_code', 0) in (529, 503, 502):
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                log.warning("Claude API overloaded (tentativa %d/4), aguardando %ds...", attempt+1, wait)
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# GERAÇÃO DO PDF (fpdf2)
# ---------------------------------------------------------------------------
def _sanitize_pdf(t: str) -> str:
    """Remove caracteres fora do latin-1 para compatibilidade com Helvetica."""
    subs = {
        '\u2014': '-', '\u2013': '-',
        '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2026': '...', '\u2022': '-',
        '\u00b7': '.', '\u2012': '-',
    }
    for ch, rep in subs.items():
        t = t.replace(ch, rep)
    return t.encode('latin-1', errors='replace').decode('latin-1')


def _gerar_pdf(diag_id: str, nome_cliente: str, report_text: str, fd: dict) -> str:
    report_text = _sanitize_pdf(report_text)
    nome_cliente = _sanitize_pdf(nome_cliente)
    os.makedirs("pdfs", exist_ok=True)
    pdf_path = f"pdfs/{diag_id}.pdf"

    pdf = FPDF()
    pdf.set_margins(left=15, top=15, right=15)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ---- CAPA ----
    pdf.set_fill_color(26, 92, 56)   # verde escuro
    pdf.rect(0, 0, 210, 297, 'F')

    pdf.set_y(60)
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_text_color(255, 255, 255)
    pdf.multi_cell(0, 12, "DIAGNÓSTICO DE GESTÃO RURAL", align="C")

    pdf.set_y(100)
    pdf.set_font("Helvetica", "", 16)
    pdf.set_text_color(232, 245, 238)
    pdf.multi_cell(0, 9, "Relatório Personalizado", align="C")

    pdf.set_y(115)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(240, 165, 0)   # amarelo
    pdf.multi_cell(0, 10, nome_cliente, align="C")

    pdf.set_y(135)
    pdf.set_font("Helvetica", "", 13)
    pdf.set_text_color(200, 230, 210)
    pdf.multi_cell(0, 8, f"Emitido em {datetime.now().strftime('%d/%m/%Y')}", align="C")

    pdf.set_y(165)
    pdf.set_font("Helvetica", "I", 11)
    pdf.set_text_color(180, 220, 195)
    pdf.multi_cell(0, 7, "Elaborado por Douglas Lemos\nMBA FGV  |  MSc Inovação Tecnológica UFSC\nHead de Novos Negócios - Agronegócio", align="C")

    pdf.set_y(240)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(150, 200, 170)
    pdf.multi_cell(0, 6, "Este diagnóstico é confidencial e destinado exclusivamente ao cliente identificado.\nReproduções não autorizadas são proibidas.", align="C")

    # ---- PÁGINAS DE CONTEÚDO ----
    pdf.add_page()
    pdf.set_fill_color(255, 255, 255)

    # Cabeçalho de página
    pdf.set_fill_color(26, 92, 56)
    pdf.rect(0, 0, 210, 14, 'F')
    pdf.set_y(3)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 8, "  DIAGNÓSTICO DE GESTÃO RURAL  |  CONFIDENCIAL", align="L")

    pdf.set_y(20)
    pdf.set_text_color(30, 30, 30)

    sections = report_text.split("##")
    for section in sections:
        if not section.strip():
            continue

        lines = section.strip().split("\n")
        title = lines[0].strip()
        body  = "\n".join(lines[1:]).strip()

        # Título da seção
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(26, 92, 56)
        pdf.set_fill_color(232, 245, 238)
        pdf.multi_cell(0, 9, title, align="L", fill=True)
        pdf.ln(2)

        # Corpo
        subsections = body.split("###")
        for sub in subsections:
            if not sub.strip():
                continue

            sub_lines = sub.strip().split("\n")
            sub_title = sub_lines[0].strip()
            sub_body  = "\n".join(sub_lines[1:]).strip()

            if len(sub_lines) > 1 and sub_title:
                pdf.set_font("Helvetica", "B", 11)
                pdf.set_text_color(45, 122, 80)
                pdf.multi_cell(0, 7, sub_title, align="L")
                pdf.set_font("Helvetica", "", 10)
                pdf.set_text_color(60, 60, 60)
                _escrever_paragrafos(pdf, sub_body)
            else:
                pdf.set_font("Helvetica", "", 10)
                pdf.set_text_color(60, 60, 60)
                _escrever_paragrafos(pdf, sub.strip())

        pdf.ln(4)

    # Rodapé da última página
    pdf.set_y(-20)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    w = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.cell(w, 5, f"Diagnostico de Gestao Rural  |  {nome_cliente}  |  {datetime.now().strftime('%d/%m/%Y')}  |  Pag. " + str(pdf.page_no()), align="C")

    pdf.output(pdf_path)
    log.info("PDF gerado: %s", pdf_path)
    return pdf_path


def _strip_inline_md(t):
    """Remove marcadores markdown: **bold**, *italic*, # headers."""
    t = t.replace("**", "")
    t = t.replace("* ", "")
    return t.strip()


def _escrever_paragrafos(pdf, texto):
    """Escreve texto com suporte a paragrafos, bullets e markdown basico."""
    lm  = pdf.l_margin
    w   = pdf.w - pdf.l_margin - pdf.r_margin
    VERDE = (26, 92, 56)
    TEXTO = (50, 50, 50)

    for linha in texto.split("\n"):
        linha = linha.strip()
        if not linha:
            pdf.ln(3)
            continue
        if linha in ("---", "***", "___"):
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            pdf.line(lm, pdf.get_y(), lm + w, pdf.get_y())
            pdf.ln(4)
            pdf.set_draw_color(0, 0, 0)
            continue
        if linha.startswith("#"):
            level = len(linha) - len(linha.lstrip("#"))
            text  = _strip_inline_md(linha.lstrip("# "))
            size  = max(13 - (level - 1) * 2, 9)
            pdf.set_x(lm)
            pdf.set_font("Helvetica", "B", size)
            pdf.set_text_color(*VERDE)
            pdf.multi_cell(w, size * 0.75, text, align="L")
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*TEXTO)
            pdf.ln(1)
            continue
        if linha.startswith("- ") or linha.startswith("* "):
            pdf.set_x(lm + 4)
            pdf.set_text_color(*TEXTO)
            pdf.multi_cell(w - 4, 6, "- " + _strip_inline_md(linha[2:]), align="L")
            continue
        if linha.startswith("**") and linha.endswith("**") and len(linha) > 4:
            pdf.set_x(lm)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(*VERDE)
            pdf.multi_cell(w, 6, linha[2:-2].strip(), align="L")
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(*TEXTO)
            continue
        pdf.set_x(lm)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*TEXTO)
        pdf.multi_cell(w, 6, _strip_inline_md(linha), align="L")
    pdf.ln(2)


# ---------------------------------------------------------------------------
# EMAIL (Gmail SMTP)
# ---------------------------------------------------------------------------
def _smtp_send(to_email: str, subject: str, html_body: str, pdf_path: str = None):
    """Envia email via Gmail SMTP com App Password."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL or GMAIL_USER
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if pdf_path:
        outer = MIMEMultipart("mixed")
        outer["Subject"] = subject
        outer["From"] = FROM_EMAIL or GMAIL_USER
        outer["To"] = to_email
        outer.attach(MIMEText(html_body, "html", "utf-8"))
        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename="Diagnostico_Gestao_Rural.pdf")
        outer.attach(part)
        msg = outer

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())


def _enviar_email_formulario(email: str, name: str, purchase_id: str, form_token: str):
    """Envia email com link do formulário via Google Apps Script (sem SMTP)."""
    import urllib.request as _urllib_req
    import json as _json

    link = f"{BASE_URL}/diagnostico/{purchase_id}"

    if not APPS_SCRIPT_WEBHOOK_URL:
        log.error("APPS_SCRIPT_WEBHOOK_URL nao configurada — email formulario NAO enviado para %s", email)
        return

    payload = _json.dumps({
        "secret": "diag-rural-wh-2026",
        "to": email,
        "name": name,
        "type": "form_link",
        "form_link": link,
    }).encode("utf-8")

    req = _urllib_req.Request(
        APPS_SCRIPT_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urllib_req.urlopen(req, timeout=30) as resp:
            result = _json.loads(resp.read())
        if result.get("ok"):
            log.info("Email formulario enviado via Apps Script para %s", email)
        else:
            log.error("Apps Script retornou erro no email formulario: %s", result.get("error"))
    except Exception as exc:
        log.exception("Falha ao enviar email formulario via Apps Script: %s", exc)


def _enviar_relatorio(email: str, name: str, pdf_path: str) -> None:
    """Envia email com PDF via Google Apps Script webhook (HTTP POST, sem SMTP)."""
    import urllib.request as _urllib_req
    import base64 as _b64
    import json as _json

    if not APPS_SCRIPT_WEBHOOK_URL:
        raise RuntimeError("APPS_SCRIPT_WEBHOOK_URL nao configurada no Railway")

    with open(pdf_path, "rb") as fh:
        pdf_b64 = _b64.b64encode(fh.read()).decode()

    payload = _json.dumps({
        "secret": "diag-rural-wh-2026",
        "to": email,
        "name": name,
        "pdf_base64": pdf_b64,
    }).encode("utf-8")

    req = _urllib_req.Request(
        APPS_SCRIPT_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urllib_req.urlopen(req, timeout=60) as resp:
        result = _json.loads(resp.read())

    if not result.get("ok"):
        raise RuntimeError(f"Apps Script erro: {result.get('error')}")

    log.info("Email enviado via Apps Script para %s", email)
def _verificar_pagamento_manual(purchase_id: str):
    """Tenta confirmar pagamento via Mercado Pago quando o usuário volta pelo back_url."""
    try:
        sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)
        result = sdk.payment().search({"external_reference": purchase_id, "status": "approved"})
        if result["status"] == 200 and result["response"]["results"]:
            payment = result["response"]["results"][0]
            form_token = str(uuid.uuid4())
            with get_db() as db:
                row = db.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
                db.execute(
                    "UPDATE purchases SET status='approved', payment_id=?, form_token=? WHERE id=?",
                    (str(payment["id"]), form_token, purchase_id)
                )
                db.commit()
            if row:
                _enviar_email_formulario(row["email"], row["name"], purchase_id, form_token)
    except Exception as e:
        log.warning("Verificação manual falhou: %s", e)


# ---------------------------------------------------------------------------
# ROTAS ESTÁTICAS / ADMIN
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/pagamento-falhou")
def pagamento_falhou():
    return render_template("pagamento_falhou.html")

@app.route("/pagamento-pendente")
def pagamento_pendente():
    purchase_id = request.args.get("external_reference", "")
    payment_id = request.args.get("payment_id", "")
    return render_template("pagamento_pendente.html",
                           purchase_id=purchase_id,
                           payment_id=payment_id)

@app.route("/check-payment")
def check_payment():
    """Endpoint para polling: verifica se pagamento foi aprovado."""
    purchase_id = request.args.get("purchase_id", "")
    payment_id = request.args.get("payment_id", "")

    if not purchase_id and not payment_id:
        return jsonify({"status": "unknown"}), 400

    # Busca por purchase_id primeiro
    if purchase_id:
        with get_db() as db:
            row = db.execute(
                "SELECT status FROM purchases WHERE id=?", (purchase_id,)
            ).fetchone()
        if row and row["status"] == "approved":
            return jsonify({"status": "approved", "redirect": f"/diagnostico/{purchase_id}"})
        elif row:
            return jsonify({"status": row["status"]})

    # Fallback: busca no MP por payment_id
    if payment_id:
        try:
            sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)
            result = sdk.payment().get(payment_id)
            if result["status"] == 200:
                payment = result["response"]
                ext_ref = payment.get("external_reference", "")
                status = payment.get("status", "pending")
                if status == "approved" and ext_ref:
                    return jsonify({"status": "approved", "redirect": f"/diagnostico/{ext_ref}"})
                return jsonify({"status": status})
        except Exception as e:
            log.warning("check-payment MP lookup falhou: %s", e)

    return jsonify({"status": "pending"})

@app.route("/admin")
def admin():
    senha = request.args.get("key", "")
    if senha != ADMIN_PASSWORD:
        abort(403)
    with get_db() as db:
        # Diagnosticos com dados de purchase join
        rows = db.execute(
            "SELECT p.id as purchase_id, p.email, p.name, p.status as pstatus, p.payment_source, p.created_at, "
            "d.id as diag_id, d.status as diag_status, d.report_text "
            "FROM purchases p "
            "LEFT JOIN diagnostics d ON d.purchase_id = p.id "
            "ORDER BY p.created_at DESC LIMIT 100"
        ).fetchall()
        # Purchases sem diagnostico
        pending = db.execute(
            "SELECT p.* FROM purchases p "
            "LEFT JOIN diagnostics d ON d.purchase_id = p.id "
            "WHERE d.id IS NULL AND p.status = 'approved' "
            "ORDER BY p.created_at DESC"
        ).fetchall()
    # Stats
    approved = [r for r in rows if r["pstatus"] == "approved"]
    total_purchases = len(set(r["purchase_id"] for r in approved if (r["payment_source"] or "") == "mercadopago"))
    total_done = len([r for r in rows if r["diag_status"] == "done"])
    total_processing = len([r for r in rows if r["diag_status"] == "processing"])
    total_error = len([r for r in rows if r["diag_status"] == "error"])
    total_real = len([r for r in rows if (r["payment_source"] or "") == "mercadopago" and r["pstatus"] == "approved"])
    receita = total_real * 97
    return render_template("admin.html",
        diagnostics=rows,
        pending_purchases=pending,
        total_purchases=total_purchases,
        total_done=total_done,
        total_processing=total_processing,
        total_error=total_error,
        receita=receita,
        admin_key=senha
    )

@app.route("/admin/seed-purchase", methods=["POST"])
def admin_seed_purchase():
    """Cria registro manual de compra aprovada (uso admin)."""
    senha = request.args.get("key", "")
    if senha != ADMIN_PASSWORD:
        abort(403)
    data = request.get_json() or {}
    email = data.get("email", "").strip()
    name = data.get("name", "").strip()
    payment_id = data.get("payment_id", "").strip()
    if not email:
        return jsonify({"error": "email obrigatorio"}), 400
    purchase_id = str(uuid.uuid4())
    form_token = str(uuid.uuid4())
    with get_db() as db:
        db.execute(
            "INSERT INTO purchases (id, email, name, payment_id, status, form_token) VALUES (?,?,?,?,?,?)",
            (purchase_id, email, name or None, payment_id or None, "approved", form_token)
        )
        db.commit()
    form_url = f"{BASE_URL}/diagnostico/{purchase_id}"
    return jsonify({"purchase_id": purchase_id, "form_url": form_url, "form_token": form_token})


@app.route("/admin/pdf/<diag_id>")
def admin_pdf(diag_id):
    """Serve ou regenera PDF do diagnostico para visualizacao admin."""
    senha = request.args.get("key", "")
    if senha != ADMIN_PASSWORD:
        abort(403)
    with get_db() as db:
        cur = db.execute(
            "SELECT d.id, d.pdf_path, d.report_text, p.name FROM diagnostics d "
            "JOIN purchases p ON p.id = d.purchase_id "
            "WHERE d.id=? OR d.purchase_id=? "
            "ORDER BY d.created_at DESC LIMIT 1", (diag_id, diag_id)
        )
        row = cur.fetchone()
    if not row:
        abort(404)
    pdf_path = row["pdf_path"]
    # Se PDF existe no disco, serve direto
    if pdf_path and os.path.exists(pdf_path):
        from flask import send_file
        return send_file(pdf_path, mimetype="application/pdf", as_attachment=False)
    # Senao, regenera do report_text salvo no banco
    if not row["report_text"]:
        abort(404)
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.close()
    form_data = {"_name": row["name"] or "Cliente"}
    try:
        new_path = _gerar_pdf(diag_id, row["name"] or "Cliente", row["report_text"], form_data)
    except Exception as e:
        log.exception("Erro ao regenerar PDF: %s", e)
        abort(500)
    from flask import send_file
    return send_file(new_path, mimetype="application/pdf", as_attachment=False)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})



@app.route("/admin/enviar-email/<diag_id>", methods=["POST"])
def admin_enviar_email(diag_id):
    """Reenvia o email com PDF para o cliente manualmente."""
    senha = request.args.get("key", "")
    if senha != ADMIN_PASSWORD:
        abort(403)
    with get_db() as db:
        cur = db.execute(
            "SELECT d.id, d.pdf_path, d.report_text, p.name, p.email FROM diagnostics d "
            "JOIN purchases p ON p.id = d.purchase_id "
            "WHERE d.id=? OR d.purchase_id=? "
            "ORDER BY d.created_at DESC LIMIT 1", (diag_id, diag_id)
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"ok": False, "erro": "Diagnostico nao encontrado"}), 404
    if not row["report_text"]:
        return jsonify({"ok": False, "erro": "Relatorio ainda nao gerado"}), 400
    pdf_path = row["pdf_path"]
    if not pdf_path or not os.path.exists(pdf_path):
        form_data = {"_name": row["name"] or "Cliente"}
        try:
            pdf_path = _gerar_pdf(row["id"], row["name"] or "Cliente", row["report_text"], form_data)
            with get_db() as db:
                db.execute("UPDATE diagnostics SET pdf_path=? WHERE id=?", (pdf_path, row["id"]))
                db.commit()
        except Exception as e:
            log.exception("Erro ao regenerar PDF: %s", e)
            return jsonify({"ok": False, "erro": f"Falha ao gerar PDF: {e}"}), 500
    import threading as _threading
    _email_local = row["email"]
    _name_local = row["name"] or "Cliente"
    _pdf_local = pdf_path
    _result = {"ok": None, "msg": None}
    def _send():
        try:
            _enviar_relatorio(_email_local, _name_local, _pdf_local)
            _result["ok"] = True
            _result["msg"] = f"Email reenviado para {_email_local}"
            log.info("Email reenviado com sucesso para %s", _email_local)
        except Exception as e:
            _result["ok"] = False
            _result["msg"] = str(e)
            log.warning("Falha ao reenviar email para %s: %s", _email_local, e)
    t = _threading.Thread(target=_send)
    t.start()
    t.join(timeout=40)
    if _result["ok"] is True:
        return jsonify({"ok": True, "msg": _result["msg"]})
    elif _result["ok"] is False:
        return jsonify({"ok": False, "erro": _result["msg"]}), 500
    else:
        return jsonify({"ok": False, "erro": "Timeout ao enviar email (>40s). Verifique SMTP."}), 500


@app.route("/meu-relatorio/<purchase_id>")
def meu_relatorio(purchase_id):
    """Area do cliente - visualizar e baixar o relatorio."""
    with get_db() as db:
        cur = db.execute(
            "SELECT p.name, p.email, p.status as pstatus, "
            "d.status as dstatus, d.id as diag_id, d.pdf_path, d.report_text "
            "FROM purchases p "
            "LEFT JOIN diagnostics d ON d.purchase_id = p.id "
            "WHERE p.id=? "
            "ORDER BY d.created_at DESC LIMIT 1", (purchase_id,)
        )
        row = cur.fetchone()
    if not row:
        abort(404)
    # Pagamento nao aprovado
    if row["pstatus"] != "approved":
        return render_template("meu_relatorio.html",
            state="pendente",
            name=row["name"] or "Cliente",
            purchase_id=purchase_id
        )
    # Relatorio ainda sendo gerado
    if not row["dstatus"] or row["dstatus"] in ("processing", "pending"):
        return render_template("meu_relatorio.html",
            state="gerando",
            name=row["name"] or "Cliente",
            purchase_id=purchase_id
        )
    # Erro na geracao
    if row["dstatus"] == "error":
        return render_template("meu_relatorio.html",
            state="erro",
            name=row["name"] or "Cliente",
            purchase_id=purchase_id
        )
    # Pronto
    return render_template("meu_relatorio.html",
        state="pronto",
        name=row["name"] or "Cliente",
        purchase_id=purchase_id,
        diag_id=row["diag_id"]
    )


@app.route("/meu-relatorio/<purchase_id>/pdf")
def meu_relatorio_pdf(purchase_id):
    """Download do PDF pelo cliente."""
    from flask import send_file
    with get_db() as db:
        cur = db.execute(
            "SELECT p.name, p.status as pstatus, "
            "d.id as diag_id, d.pdf_path, d.report_text, d.status as dstatus "
            "FROM purchases p "
            "LEFT JOIN diagnostics d ON d.purchase_id = p.id "
            "WHERE p.id=? "
            "ORDER BY d.created_at DESC LIMIT 1", (purchase_id,)
        )
        row = cur.fetchone()
    if not row or row["pstatus"] != "approved" or row["dstatus"] != "done":
        abort(404)
    pdf_path = row["pdf_path"]
    if not pdf_path or not os.path.exists(pdf_path):
        if not row["report_text"]:
            abort(404)
        form_data = {"_name": row["name"] or "Cliente"}
        try:
            pdf_path = _gerar_pdf(row["diag_id"], row["name"] or "Cliente", row["report_text"], form_data)
            with get_db() as db:
                db.execute("UPDATE diagnostics SET pdf_path=? WHERE id=?", (pdf_path, row["diag_id"]))
                db.commit()
        except Exception as e:
            log.exception("Erro ao regenerar PDF para cliente: %s", e)
            abort(500)
    name_slug = (row["name"] or "relatorio").replace(" ", "_")[:30]
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=True,
                     download_name=f"diagnostico_{name_slug}.pdf")


@app.route("/admin/mp-verificar/<payment_id>")
def admin_mp_verificar(payment_id):
    """Consulta o Mercado Pago para verificar status real de um pagamento."""
    senha = request.args.get("key", "")
    if senha != ADMIN_PASSWORD:
        abort(403)
    if not payment_id or payment_id == "None":
        return jsonify({"ok": False, "erro": "payment_id nao informado"}), 400
    try:
        sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)
        result = sdk.payment().get(payment_id)
        if result["status"] != 200:
            return jsonify({"ok": False, "erro": f"MP retornou status {result['status']}"}), 400
        p = result["response"]
        return jsonify({
            "ok": True,
            "status": p.get("status"),
            "status_detail": p.get("status_detail"),
            "valor": p.get("transaction_amount"),
            "moeda": p.get("currency_id"),
            "pagador_email": p.get("payer", {}).get("email"),
            "data": p.get("date_approved") or p.get("date_created"),
            "descricao": p.get("description"),
            "metodo": p.get("payment_type_id"),
        })
    except Exception as e:
        log.exception("Erro ao verificar MP: %s", e)
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/admin/mp-pagamentos")
def admin_mp_pagamentos():
    """Lista os ultimos pagamentos aprovados diretamente do Mercado Pago."""
    senha = request.args.get("key", "")
    if senha != ADMIN_PASSWORD:
        abort(403)
    try:
        sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)
        filtros = {
            "status": "approved",
            "sort": "date_created",
            "criteria": "desc",
            "range": "date_created",
            "begin_date": "NOW-30DAYS",
            "end_date": "NOW",
        }
        result = sdk.payment().search(filtros)
        if result["status"] != 200:
            return jsonify({"ok": False, "erro": "Falha ao buscar MP"}), 400
        pagamentos = result["response"].get("results", [])
        resumo = [{
            "id": str(p.get("id")),
            "status": p.get("status"),
            "valor": p.get("transaction_amount"),
            "pagador": p.get("payer", {}).get("email"),
            "data": p.get("date_approved") or p.get("date_created"),
            "external_reference": p.get("external_reference"),
            "descricao": p.get("description"),
        } for p in pagamentos]
        total_real = sum(p["valor"] or 0 for p in resumo)
        return jsonify({"ok": True, "total": len(resumo), "receita_real": total_real, "pagamentos": resumo})
    except Exception as e:
        log.exception("Erro ao buscar pagamentos MP: %s", e)
        return jsonify({"ok": False, "erro": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
