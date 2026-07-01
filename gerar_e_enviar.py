#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agente diario de TRENDS de video da PBR Brazil.
Roda na nuvem (GitHub Actions) todo dia as 6h (horario de Brasilia).

Fluxo:
  1. Busca os trends em alta no Brasil hoje (Google Trends).
  2. Le o ranking oficial da PBR e a performance do Instagram (apoio).
  3. Le o historico (historico.md) para NUNCA repetir ideia.
  4. A IA cria 3 ideias DETALHADAS, cada uma partindo de um trend e adaptada
     pra PBR usando um competidor de sucesso (sem o atleta precisar gravar).
  5. Pra cada ideia, acha um reel REAL do Instagram (via Serper/Google) e manda
     o link direto junto.
  6. Envia no WhatsApp via CallMeBot (uma mensagem detalhada por ideia).
  7. Acrescenta as 3 ideias ao historico (o GitHub Actions comita de volta).

Variaveis de ambiente (Secrets no GitHub):
  ANTHROPIC_API_KEY     -> chave da API da Anthropic (obrigatoria)
  CALLMEBOT_APIKEY      -> API key do CallMeBot (obrigatoria)
  PHONE                 -> numero de destino, ex: +5512997416438 (obrigatoria)
  SERPER_API_KEY        -> chave da API Serper.dev p/ achar reels (opcional)
  SUPERMETRICS_API_KEY  -> chave da API REST do Supermetrics (opcional)
  SUPERMETRICS_DS_USER  -> id do login do Supermetrics, se necessario (opcional)
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from anthropic import Anthropic

# ----------------------------------------------------------------------------
# Configuracao
# ----------------------------------------------------------------------------
MODELO = "claude-sonnet-4-6"
HISTORICO = "historico.md"
FUSO_BRASILIA = timezone(timedelta(hours=-3))

# Supermetrics - Instagram Insights da PBR Brazil
SM_ENDPOINT = "https://api.supermetrics.com/enterprise/v2/query/data/json"
SM_DS_ID = "IGI"
SM_CONTA_PBR = "17841401478253574"
SM_FIELDS = (
    "media_permalink,timestamp,media_caption,media_like_count,"
    "media_comments_count,media_reach,media_saved,media_shares"
)

RANKING_URL = "https://pbrbrazil.com/series/etapas/standings/"
TRENDS_URL = "https://trends.google.com/trending/rss?geo=BR"
SERPER_ENDPOINT = "https://google.serper.dev/search"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY")
PHONE = os.environ.get("PHONE")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
SUPERMETRICS_API_KEY = os.environ.get("SUPERMETRICS_API_KEY")
SUPERMETRICS_DS_USER = os.environ.get("SUPERMETRICS_DS_USER")

if not all([ANTHROPIC_API_KEY, CALLMEBOT_APIKEY, PHONE]):
    sys.exit("ERRO: faltam variaveis obrigatorias (ANTHROPIC_API_KEY, CALLMEBOT_APIKEY, PHONE).")


# ----------------------------------------------------------------------------
# Coleta de dados
# ----------------------------------------------------------------------------
def ler_historico():
    try:
        with open(HISTORICO, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def puxar_trends():
    """Assuntos em alta no Brasil hoje (Google Trends, RSS publico)."""
    try:
        resp = requests.get(
            TRENDS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60
        )
        if resp.status_code != 200:
            print("Aviso: Google Trends status %s" % resp.status_code)
            return ""
        titulos = re.findall(r"<title>(.*?)</title>", resp.text, re.S)
        termos = [html.unescape(t).strip() for t in titulos[1:19] if t.strip()]
        return ", ".join(termos)
    except Exception as e:  # noqa: BLE001
        print("Aviso: falha ao buscar trends: %s" % e)
        return ""


def puxar_ranking_pbr():
    """Ranking oficial da PBR Brazil (top atletas, do HTML do site)."""
    try:
        resp = requests.get(
            RANKING_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60
        )
        if resp.status_code != 200:
            print("Aviso: ranking PBR status %s" % resp.status_code)
            return ""
        m = re.search(r"<tbody>(.*?)</tbody>", resp.text, re.S | re.I)
        bloco = m.group(1) if m else resp.text
        texto = re.sub(r"<[^>]+>", " ", bloco)
        texto = html.unescape(texto)
        texto = re.sub(r"\s+", " ", texto).strip()
        return texto[:2500] if texto else ""
    except Exception as e:  # noqa: BLE001
        print("Aviso: falha ao ler ranking PBR: %s" % e)
        return ""


def puxar_dados_pbr():
    """Performance dos posts da PBR via API REST do Supermetrics (apoio)."""
    if not SUPERMETRICS_API_KEY:
        print("Aviso: SUPERMETRICS_API_KEY nao configurada - seguindo sem dados reais.")
        return ""
    hoje = datetime.now(FUSO_BRASILIA)
    inicio = (hoje - timedelta(days=30)).strftime("%Y-%m-%d")
    fim = hoje.strftime("%Y-%m-%d")
    consulta = {
        "api_key": SUPERMETRICS_API_KEY,
        "ds_id": SM_DS_ID,
        "ds_accounts": SM_CONTA_PBR,
        "start_date": inicio,
        "end_date": fim,
        "fields": SM_FIELDS,
        "order_rows": "media_like_count desc",
        "max_rows": 40,
    }
    if SUPERMETRICS_DS_USER:
        consulta["ds_user"] = SUPERMETRICS_DS_USER
    try:
        resp = requests.get(
            SM_ENDPOINT, params={"json": json.dumps(consulta)}, timeout=180
        )
        if resp.status_code != 200:
            print("Aviso: Supermetrics status %s: %s" % (resp.status_code, resp.text[:300]))
            return ""
        return resp.text[:8000]
    except Exception as e:  # noqa: BLE001
        print("Aviso: falha ao chamar o Supermetrics: %s" % e)
        return ""


def achar_reel_instagram(termo):
    """Acha um reel REAL do Instagram sobre o trend (via Serper/Google).

    Retorna a URL direta do reel, ou um link de hashtag como fallback, ou "".
    """
    if not termo:
        return ""
    if SERPER_API_KEY:
        for consulta in (termo + " site:instagram.com/reel", termo + " instagram reel"):
            try:
                resp = requests.post(
                    SERPER_ENDPOINT,
                    headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                    json={"q": consulta, "gl": "br", "hl": "pt-br", "num": 10},
                    timeout=30,
                )
                if resp.status_code != 200:
                    print("Aviso: Serper status %s" % resp.status_code)
                    continue
                for item in resp.json().get("organic", []):
                    link = item.get("link", "")
                    if "instagram.com/reel" in link or "instagram.com/p/" in link:
                        return link.split("?")[0]
            except Exception as e:  # noqa: BLE001
                print("Aviso: falha ao buscar reel: %s" % e)
    # fallback: hashtag da 1a palavra do termo (existe de verdade no IG)
    palavras = re.sub(r"[^a-z0-9 ]", " ", termo.lower()).split()
    tag = palavras[0] if palavras else "rodeio"
    return "https://www.instagram.com/explore/tags/%s/" % tag


# ----------------------------------------------------------------------------
# Geracao das ideias (IA)
# ----------------------------------------------------------------------------
def gerar_ideias(historico, dados_pbr, ranking, trends, data_str):
    """Pede a IA 3 ideias DETALHADAS. Retorna dict com 'ideias' e 'historico'."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    sistema = (
        "Voce e um especialista em TRENDS virais de Instagram (Reels) aplicados ao "
        "nicho de montaria em touros / PBR Brazil. Sua especialidade: pegar um TREND "
        "viral do momento (um formato de reel em alta, um audio/musica, um meme, um "
        "desafio, ou um assunto que esta bombando) e adaptar pro cenario da PBR "
        "Brazil, usando um competidor de sucesso como personagem. "
        "RESTRICAO: os atletas NAO estao disponiveis pra gravar nada novo - NADA de "
        "dancinha, coreografia, ou o atleta participando do trend ao vivo. A execucao "
        "usa material que a PBR ja tem: imagens de arquivo, montarias antigas, cortes, "
        "audio em alta por cima de clipes, texto na tela, comparacoes e narracao. "
        "Voce escreve BRIEFINGS DETALHADOS pra um videomaker executar sem duvidas. "
        "Escreva em portugues do Brasil com acentuacao correta."
    )

    bloco_trends = (
        ("ASSUNTOS EM ALTA NO BRASIL HOJE (Google Trends):\n" + trends + "\n")
        if trends else
        "OBS: nao consegui puxar os trends de hoje - use seu conhecimento dos "
        "formatos de reel virais atuais.\n"
    )
    bloco_ranking = (
        ("\nAPOIO - Ranking oficial da PBR (use SO pra escolher um competidor de "
         "sucesso pra encaixar no trend):\n" + ranking + "\n") if ranking else ""
    )
    bloco_dados = (
        ("\nAPOIO - performance dos posts da PBR no Instagram (use SO pra saber que "
         "tipo de conteudo ressoa):\n" + dados_pbr + "\n") if dados_pbr else ""
    )

    instrucao = (
        "Gere EXATAMENTE 3 ideias NOVAS de reel para hoje (" + data_str + ").\n\n"
        "O FOCO #1 SAO OS TRENDS. Cada ideia PARTE de um trend viral e ADAPTA pro "
        "cenario da PBR Brazil, com um competidor de sucesso como personagem.\n\n"
        + bloco_trends + bloco_ranking + bloco_dados
        + "\nLEMBRETE: atletas NAO gravam nada - use so arquivo, edicao, audio sobre "
        "clipes, texto na tela, comparacoes e narracao.\n\n"
        "REGRA: nao repita nem faca variacao obvia de nada do historico:\n"
        "--- HISTORICO ---\n"
        + (historico or "(vazio)")
        + "\n--- FIM DO HISTORICO ---\n\n"
        "IMPORTANTISSIMO: cada ideia tem que vir MUITO DETALHADA, um briefing "
        "completo pro videomaker executar sem duvidas. Nada de resumo curto. "
        "Use a ferramenta 'enviar_ideias'."
    )

    ferramenta = {
        "name": "enviar_ideias",
        "description": "Envia as 3 ideias detalhadas de reel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ideias": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "titulo": {
                                "type": "string",
                                "description": "Titulo curto e chamativo do reel.",
                            },
                            "trend": {
                                "type": "string",
                                "description": (
                                    "Qual e o trend viral de origem (formato/audio/"
                                    "meme/assunto) e por que esta em alta."
                                ),
                            },
                            "busca": {
                                "type": "string",
                                "description": (
                                    "Termo de busca GENERICO e simples (2-4 palavras) "
                                    "que REALMENTE tenha reels no Instagram pra servir "
                                    "de referencia visual do FORMATO. Foque no nicho "
                                    "(montaria, touro, rodeio, PBR, peao) + o formato do "
                                    "trend. Ex: 'montaria touro comparacao', 'rodeio "
                                    "forca bruta', 'touro 8 segundos', 'peao arena "
                                    "bastidor'. NAO use nomes especificos de jogos, "
                                    "times, eventos ou pessoas do dia - isso nao tem "
                                    "reel. Sem hashtag, sem aspas."
                                ),
                            },
                            "brief": {
                                "type": "string",
                                "description": (
                                    "Briefing DETALHADO pro videomaker, em TOPICOS "
                                    "curtos (nao paragrafo corrido), OBJETIVO e no "
                                    "maximo ~1600 caracteres. Inclua: Gancho (3s) com o "
                                    "texto na tela; Roteiro cena a cena (o que aparece e "
                                    "quando); Audio/musica; Imagens de arquivo/montarias "
                                    "e qual competidor usar (cite o nome do ranking); "
                                    "Duracao; e Legenda/CTA. Detalhado mas direto - o "
                                    "videomaker tem que entender tudo sem duvida."
                                ),
                            },
                        },
                        "required": ["titulo", "trend", "busca", "brief"],
                    },
                },
                "historico": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "As 3 ideias resumidas em 1 linha cada (Titulo - resumo).",
                },
            },
            "required": ["ideias", "historico"],
        },
    }

    resp = client.messages.create(
        model=MODELO,
        max_tokens=8000,
        system=sistema,
        tools=[ferramenta],
        tool_choice={"type": "tool", "name": "enviar_ideias"},
        messages=[{"role": "user", "content": instrucao}],
    )
    for bloco in resp.content:
        if getattr(bloco, "type", None) == "tool_use":
            dados = bloco.input
            if "ideias" in dados and dados["ideias"]:
                return dados
    raise RuntimeError("A IA nao retornou as ideias no formato esperado (stop=%s)." % resp.stop_reason)


# ----------------------------------------------------------------------------
# Envio (WhatsApp via CallMeBot)
# ----------------------------------------------------------------------------
def _dividir_mensagem(texto, limite=750):
    """Quebra a mensagem em partes (o CallMeBot trunca mensagens longas)."""
    if len(texto) <= limite:
        return [texto]
    partes, atual = [], ""
    for linha in texto.split("\n"):
        candidato = (atual + "\n" + linha) if atual else linha
        if len(candidato) > limite and atual:
            partes.append(atual)
            atual = linha
        else:
            atual = candidato
    if atual:
        partes.append(atual)
    return partes


def _enviar_parte(texto):
    resp = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": PHONE, "apikey": CALLMEBOT_APIKEY, "text": texto},
        timeout=60,
    )
    print("Resposta CallMeBot:", resp.text[:200])
    if "queued" not in resp.text.lower() and "sent" not in resp.text.lower():
        sys.exit("ERRO: CallMeBot nao confirmou o envio.")


def enviar_whatsapp(mensagem):
    partes = _dividir_mensagem(mensagem)
    total = len(partes)
    for i, parte in enumerate(partes, 1):
        if total > 1:
            parte = "(cont. %d/%d)\n%s" % (i, total, parte)
        _enviar_parte(parte)
        time.sleep(6)  # respeita o limite de frequencia do CallMeBot


def montar_mensagem_ideia(indice, total, ideia, link):
    linhas = [
        "IDEIA %d/%d - %s" % (indice, total, ideia.get("titulo", "")),
        "",
        "Trend: " + ideia.get("trend", ""),
    ]
    if link:
        linhas += ["", "Referencia (reel): " + link]
    linhas += ["", "Como fazer:", ideia.get("brief", "")]
    return "\n".join(linhas)


def salvar_historico(linhas, data_str):
    bloco = "\n## " + data_str + "\n" + "".join(
        "%d. %s\n" % (i, linha) for i, linha in enumerate(linhas, 1)
    )
    with open(HISTORICO, "a", encoding="utf-8") as f:
        f.write(bloco)


# ----------------------------------------------------------------------------
def main():
    hoje = datetime.now(FUSO_BRASILIA)
    data_iso = hoje.strftime("%Y-%m-%d")
    data_br = hoje.strftime("%d/%m")

    historico = ler_historico()
    trends = puxar_trends()
    dados_pbr = puxar_dados_pbr()
    ranking = puxar_ranking_pbr()
    dados = gerar_ideias(historico, dados_pbr, ranking, trends, data_br)

    ideias = dados["ideias"]
    total = len(ideias)

    # 1) cabecalho
    enviar_whatsapp(
        "PBR Brazil - Trends de hoje (%s)\n"
        "Seguem %d ideias detalhadas (uma por mensagem):" % (data_br, total)
    )
    # 2) uma mensagem detalhada por ideia, com o link do reel
    for i, ideia in enumerate(ideias, 1):
        link = achar_reel_instagram(ideia.get("busca", ""))
        print("Ideia %d - busca '%s' -> reel: %s" % (i, ideia.get("busca", ""), link))
        enviar_whatsapp(montar_mensagem_ideia(i, total, ideia, link))

    salvar_historico(dados["historico"], data_iso)
    print("OK - %d ideias enviadas e historico atualizado." % total)


if __name__ == "__main__":
    main()
