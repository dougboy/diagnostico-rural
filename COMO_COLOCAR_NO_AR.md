# Como colocar o sistema no ar
## Tempo estimado: 40 minutos. Faça uma vez. Depois é automático.

---

## PASSO 1 — Anthropic API (Claude) — 5 minutos

1. Acesse: https://console.anthropic.com
2. Crie uma conta (pode usar seu email do Google)
3. Vá em **"API Keys"** → clique **"Create Key"**
4. Copie a chave (começa com `sk-ant-...`)
5. Guarde — vamos usar no Passo 4

**Custo:** ~R$0,10 a R$0,50 por relatório gerado. Os primeiros $5 de crédito são gratuitos (cerca de 50 relatórios).

---

## PASSO 2 — Mercado Pago — 10 minutos

1. Acesse: https://www.mercadopago.com.br
2. Crie uma conta com seu CPF (ou use se já tiver)
3. Vá em: **Seu negócio → Configurações → Credenciais**
4. Clique em **"Credenciais de produção"**
5. Copie o **"Access Token"** (começa com `APP_USR-...`)
6. Guarde — vamos usar no Passo 4

**Importante:** Para receber pagamentos reais (não apenas testar), sua conta precisa ter CPF verificado e conta bancária cadastrada. Isso é feito na mesma área de Credenciais.

---

## PASSO 3 — Resend (email automático) — 5 minutos

1. Acesse: https://resend.com
2. Crie uma conta gratuita
3. Vá em **"API Keys"** → **"Create API Key"**
4. Copie a chave (começa com `re_...`)
5. Guarde — vamos usar no Passo 4

**Importante sobre o email remetente:** No plano gratuito do Resend, você pode enviar de `onboarding@resend.dev` para testes. Para usar seu próprio domínio (ex: `diagnostico@seudominio.com.br`), você precisa verificar o domínio no painel do Resend — processo simples, leva 5 minutos e eles explicam passo a passo.

**Custo:** Gratuito até 3.000 emails/mês. Mais que suficiente para começar.

---

## PASSO 4 — Deploy no Railway — 15 minutos

### 4.1 Criar conta no Railway
1. Acesse: https://railway.app
2. Faça login com sua conta do GitHub (crie uma se não tiver — é gratuito)

### 4.2 Subir o código para o GitHub
1. Acesse: https://github.com e crie um repositório privado chamado `agro-diagnostico`
2. Faça upload da pasta `agro-diagnostico` que está na sua pasta "Empreendedor"
   - No GitHub: clique **"uploading an existing file"**
   - Arraste todos os arquivos da pasta (app.py, requirements.txt, Procfile, railway.toml, templates/, static/)
   - Clique **"Commit changes"**

### 4.3 Deploy no Railway
1. No Railway: clique **"New Project"** → **"Deploy from GitHub repo"**
2. Selecione o repositório `agro-diagnostico`
3. O Railway vai detectar que é Python e iniciar o build automaticamente
4. Aguarde o deploy (2-3 minutos)
5. Quando terminar, clique em **"Settings"** → **"Domains"** → **"Generate Domain"**
6. Você vai receber uma URL tipo: `agro-diagnostico-production.up.railway.app`
7. **Guarde essa URL** — é o endereço do seu sistema

### 4.4 Configurar as variáveis de ambiente
1. No Railway, vá em **"Variables"** → **"Add Variable"**
2. Adicione cada variável abaixo com os valores que você copiou nos passos anteriores:

| Variável | Valor |
|----------|-------|
| `ANTHROPIC_API_KEY` | Sua chave da Anthropic (sk-ant-...) |
| `MERCADOPAGO_ACCESS_TOKEN` | Sua chave do Mercado Pago (APP_USR-...) |
| `RESEND_API_KEY` | Sua chave do Resend (re_...) |
| `FROM_EMAIL` | `onboarding@resend.dev` (para começar) ou seu email verificado |
| `BASE_URL` | `https://agro-diagnostico-production.up.railway.app` (a URL do passo 4.3) |
| `SECRET_KEY` | Qualquer string aleatória (ex: `minha-chave-super-secreta-2026`) |
| `PRODUCT_PRICE` | `97` |
| `ADMIN_PASSWORD` | Uma senha que só você vai saber |

3. Após adicionar todas as variáveis, o Railway vai fazer um re-deploy automático

---

## PASSO 5 — Teste final — 5 minutos

1. Acesse a URL do seu sistema (ex: `https://agro-diagnostico-production.up.railway.app`)
2. Você deve ver a landing page funcionando
3. Para testar sem gastar dinheiro: no Mercado Pago, vá em **"Credenciais de teste"** e use o Access Token de teste. Assim você pode simular uma compra completa.
4. Acesse o painel admin: `https://sua-url.railway.app/admin?key=SUA_ADMIN_PASSWORD`

---

## PASSO 6 — Configurar o webhook do Mercado Pago — 5 minutos

Para que o sistema receba confirmações de pagamento automaticamente:

1. Acesse o Mercado Pago → **Configurações → Notificações webhook**
2. Em URL: cole `https://sua-url.railway.app/webhook/mercadopago`
3. Eventos: marque **"Payments"**
4. Salve

---

## PRONTO! O sistema está no ar.

**O que acontece automaticamente a partir de agora:**
1. Cliente acessa seu site → preenche nome e email → clica em pagar
2. Mercado Pago processa o pagamento (cartão, Pix ou boleto)
3. Webhook confirma → sistema envia o link do formulário por email
4. Cliente preenche o formulário → envia
5. Sistema chama a API da Claude → gera o relatório de 12-15 páginas
6. PDF formatado → enviado por email automaticamente
7. Você não precisa fazer nada

**Acompanhe pelo painel admin:**
`https://sua-url.railway.app/admin?key=SUA_SENHA_ADMIN`

---

## Custos mensais após o setup

| Item | Custo |
|------|-------|
| Railway (hosting) | Gratuito até 500h/mês (suficiente) |
| Resend (email) | Gratuito até 3.000 emails/mês |
| Anthropic API | ~R$0,30 por relatório gerado |
| Mercado Pago | 4,99% por transação (descontado do valor recebido) |
| **Total fixo** | **R$ 0/mês** |
| Por relatório vendido a R$97 | R$92,15 líquidos após taxa do MP |

---

## Para direcionar clientes para o sistema

Use o link da sua landing page em:
- Perfil do LinkedIn (na seção "Em destaque")
- Bio do Instagram
- Grupos do Facebook de agronegócio
- Campanhas de Meta Ads (quando for escalar)
- Assinatura de email profissional

O link é simplesmente: `https://sua-url.railway.app`

---

## Dúvidas ou problemas?

Se encontrar algum erro no deploy ou nas configurações, o log de erros está disponível no Railway → clique no serviço → aba "Logs".
