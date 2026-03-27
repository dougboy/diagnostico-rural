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
                "UPDATE purchases SET status='approved', payment_id=?, form_token=? WHERE id=?",
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


def _escrever_paragrafos(pdf: FPDF, texto: str):
    """Escreve texto com suporte a parágrafos e bullets."""
    lm = pdf.l_margin
    w = pdf.w - pdf.l_margin - pdf.r_margin
    for linha in texto.split("\n"):
        linha = linha.strip()
        if not linha:
            pdf.ln(3)
            continue
        pdf.set_x(lm)
        if linha.startswith("- ") or linha.startswith("• "):
            pdf.set_x(lm + 4)
            pdf.multi_cell(w - 4, 6, "• " + linha[2:], align="L")
        elif linha.startswith("**") and linha.endswith("**"):
            pdf.set_x(lm)
            pdf.set_font("Helvetica", "B", 10)
            clean = linha[2:-2] if linha.startswith("**") and linha.endswith("**") else linha
            pdf.multi_cell(w, 6, clean, align="L")
            pdf.set_font("Helvetica", "", 10)
        else:
            pdf.set_x(lm)
            pdf.multi_cell(w, 6, linha, align="L")
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

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())


def _enviar_email_formulario(email: str, name: str, purchase_id: str, form_token: str):
    link = f"{BASE_URL}/diagnostico/{purchase_id}"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:580px;margin:0 auto;padding:32px 24px">
      <div style="background:#1A5C38;padding:20px;border-radius:8px 8px 0 0;text-align:center">
        <h2 style="color:#fff;margin:0">Pagamento confirmado!</h2>
      </div>
      <div style="background:#fff;border:1px solid #e5e5e5;border-top:none;padding:28px;border-radius:0 0 8px 8px">
        <p>Olá, <strong>{name}</strong>!</p>
        <p>Seu pagamento foi aprovado. Agora é só preencher o formulário de diagnóstico para eu gerar seu relatório personalizado.</p>
        <p>O processo leva cerca de <strong>10 a 15 minutos</strong>. Quanto mais detalhado você for, mais preciso e útil será o relatório.</p>
        <div style="text-align:center;margin:32px 0">
          <a href="{link}" style="background:#F0A500;color:#1A1A2E;text-decoration:none;padding:16px 32px;border-radius:8px;font-weight:700;font-size:16px">
            PREENCHER O FORMULÁRIO
          </a>
        </div>
        <p style="color:#777;font-size:13px">Link válido para uso imediato. Se tiver qualquer problema, responda este email.</p>
        <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
        <p style="font-size:13px;color:#999">Douglas Lemos<br>MBA FGV | MSc Inovação UFSC</p>
      </div>
    </div>
    """
    _smtp_send(email, "Pagamento confirmado - Preencha seu diagnostico", html)


def _enviar_relatorio(email: str, name: str, pdf_path: str):
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:580px;margin:0 auto;padding:32px 24px">
      <div style="background:#1A5C38;padding:20px;border-radius:8px 8px 0 0;text-align:center">
        <h2 style="color:#fff;margin:0">Seu relatório está em anexo</h2>
      </div>
      <div style="background:#fff;border:1px solid #e5e5e5;border-top:none;padding:28px;border-radius:0 0 8px 8px">
        <p>Olá, <strong>{name}</strong>!</p>
        <p>Seu <strong>Diagnóstico de Gestão Rural</strong> personalizado está em anexo a este email.</p>
        <p>O relatório inclui:</p>
        <ul>
          <li>Análise da sua situação atual por área de gestão</li>
          <li>Pontos críticos e riscos identificados</li>
          <li>Plano de ação priorizado (30 dias, 2-6 meses, 6-24 meses)</li>
        </ul>
        <p>Se tiver dúvidas sobre qualquer ponto do relatório, responda este email diretamente.</p>
        <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
        <p style="font-size:13px;color:#999">Douglas Lemos<br>MBA FGV | MSc Inovação UFSC<br>Head de Novos Negócios - Agronegócio</p>
      </div>
    </div>
    """
    _smtp_send(email, "Seu Diagnostico de Gestao Rural esta pronto", html, pdf_path=pdf_path)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
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
    return render_template("pagamento_pendente.html")

@app.route("/admin")
def admin():
    senha = request.args.get("key", "")
    if senha != ADMIN_PASSWORD:
        abort(403)
    with get_db() as db:
        purchases   = db.execute("SELECT * FROM purchases ORDER BY created_at DESC LIMIT 50").fetchall()
        diagnostics = db.execute("SELECT * FROM diagnostics ORDER BY created_at DESC LIMIT 50").fetchall()
    return render_template("admin.html", purchases=purchases, diagnostics=diagnostics)

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


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
