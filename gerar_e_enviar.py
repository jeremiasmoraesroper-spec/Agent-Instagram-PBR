#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agente diario de ideias de video da PBR Brazil.
Roda na nuvem (GitHub Actions) todo dia as 6h (horario de Brasilia).

Fluxo:
  1. Puxa a performance real dos posts/reels da PBR Brazil via API REST do Supermetrics.
  2. Le o historico de ideias ja enviadas (historico.md) para NUNCA repetir.
  3. Pede a IA (Claude) 3 ideias NOVAS de video baseadas no que esta performando.
  4. Envia a mensagem no WhatsApp via CallMeBot.
  5. Acrescenta as 3 ideias ao historico (o GitHub Actions comita de volta).

Variaveis de ambiente (Secrets no GitHub):
  ANTHROPIC_API_KEY     -> chave da API da Anthropic (obrigatoria)
  CALLMEBOT_APIKEY      -> API key do CallMeBot (obrigatoria)
  PHONE                 -> numero de destino, ex: +5512997416438 (obrigatoria)
  SUPERMETRICS_API_KEY  -> chave da API REST do Supermetrics (opcional)
  SUPERMETRICS_DS_USER  -> id do login do Supermetrics, se necessario (opcional)

Se SUPERMETRICS_API_KEY nao estiver configurada (ou a chamada falhar), o agente
continua funcionando so com trends/conhecimento de nicho e avisa na mensagem.
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from anthropic import Anthropic

# ----------------------------------------------------------------------------
# Configuracao
# ----------------------------------------------------------------------------
MODELO = "claude-sonnet-4-6"          # bom equilibrio qualidade/custo
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

# Ranking oficial da PBR Brazil (dados no HTML da pagina)
RANKING_URL = "https://pbrbrazil.com/series/etapas/standings/"

# Assuntos em alta no Brasil hoje (Google Trends - RSS publico)
TRENDS_URL = "https://trends.google.com/trending/rss?geo=BR"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY")
PHONE = os.environ.get("PHONE")
SUPERMETRICS_API_KEY = os.environ.get("SUPERMETRICS_API_KEY")
SUPERMETRICS_DS_USER = os.environ.get("SUPERMETRICS_DS_USER")

if not all([ANTHROPIC_API_KEY, CALLMEBOT_APIKEY, PHONE]):
    sys.exit("ERRO: faltam variaveis obrigatorias (ANTHROPIC_API_KEY, CALLMEBOT_APIKEY, PHONE).")


def ler_historico():
    try:
        with open(HISTORICO, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def puxar_dados_pbr():
    """Puxa a performance real dos posts da PBR via API REST do Supermetrics.

    Retorna um texto (JSON) com os dados, ou "" se indisponivel.
    """
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
        # Devolve o JSON cru (truncado); a IA interpreta os melhores posts.
        return resp.text[:8000]
    except Exception as e:  # noqa: BLE001
        print("Aviso: falha ao chamar o Supermetrics: %s" % e)
        return ""


def puxar_ranking_pbr():
    """Le o ranking oficial da PBR Brazil (atletas, pontos, stats) do site.

    Os dados vem no HTML da pagina (tabela). Retorna texto limpo do topo do
    ranking, ou "" se indisponivel.
    """
    try:
        resp = requests.get(
            RANKING_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60
        )
        if resp.status_code != 200:
            print("Aviso: ranking PBR status %s" % resp.status_code)
            return ""
        m = re.search(r"<tbody>(.*?)</tbody>", resp.text, re.S | re.I)
        bloco = m.group(1) if m else resp.text
        texto = re.sub(r"<[^>]+>", " ", bloco)      # remove tags
        texto = html.unescape(texto)                # decodifica entidades
        texto = re.sub(r"\s+", " ", texto).strip()  # normaliza espacos
        # Colunas: Classificacao, Competidor, Pais, Eventos, Montarias/Paradas,
        # % Paradas, Dinheiro, Pontos, Diferenca do Lider.
        return texto[:2500] if texto else ""
    except Exception as e:  # noqa: BLE001
        print("Aviso: falha ao ler ranking PBR: %s" % e)
        return ""


def puxar_trends():
    """Busca os assuntos em alta no Brasil hoje (Google Trends, RSS publico)."""
    try:
        resp = requests.get(
            TRENDS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60
        )
        if resp.status_code != 200:
            print("Aviso: Google Trends status %s" % resp.status_code)
            return ""
        titulos = re.findall(r"<title>(.*?)</title>", resp.text, re.S)
        # o 1o titulo e o nome do feed; pega os proximos ~18 termos em alta
        termos = [html.unescape(t).strip() for t in titulos[1:19] if t.strip()]
        return ", ".join(termos)
    except Exception as e:  # noqa: BLE001
        print("Aviso: falha ao buscar trends: %s" % e)
        return ""


def gerar_ideias(historico, dados_pbr, ranking, trends, data_str):
    """Pede a IA 3 ideias novas. Retorna dict com 'whatsapp' e 'historico'."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    sistema = (
        "Voce e um especialista em TRENDS virais de redes sociais (Reels do "
        "Instagram, TikTok) aplicados ao nicho de montaria em touros / PBR Brazil. "
        "Sua especialidade: pegar um TREND viral do momento (um formato de video, "
        "um audio/musica em alta, um meme, um desafio, ou um assunto que esta "
        "bombando) e adaptar pro cenario da PBR Brazil, usando um competidor de "
        "sucesso como personagem. "
        "RESTRICAO IMPORTANTE: os atletas NAO estao disponiveis pra gravar nada "
        "novo - entao NADA de dancinha, coreografia, ou o atleta participando do "
        "trend ao vivo. A execucao tem que funcionar com material que a PBR ja "
        "tem: imagens de arquivo, montarias antigas, cortes, audio em alta por "
        "cima de clipes existentes, texto na tela, comparacoes e narracao. "
        "Escreva sempre em portugues do Brasil com acentuacao correta e use "
        "poucos emojis."
    )

    # FONTE PRINCIPAL: trends.
    if trends:
        bloco_trends = (
            "ASSUNTOS EM ALTA NO BRASIL HOJE (Google Trends):\n" + trends + "\n"
        )
    else:
        bloco_trends = (
            "OBS: nao consegui puxar os trends de hoje - use seu conhecimento dos "
            "formatos de reel virais atuais.\n"
        )

    # FONTES DE APOIO (so pra escolher o competidor e o que ressoa):
    if ranking:
        bloco_ranking = (
            "\nAPOIO - Ranking oficial atual da PBR Brazil (use SO pra escolher um "
            "competidor de sucesso pra encaixar no trend; colunas: Classificacao, "
            "Competidor, Pais, Eventos, Montarias/Paradas, % Paradas, Dinheiro, "
            "Pontos, Diferenca do Lider):\n" + ranking + "\n"
        )
    else:
        bloco_ranking = ""

    if dados_pbr:
        bloco_dados = (
            "\nAPOIO - performance dos posts recentes da PBR no Instagram (JSON do "
            "Supermetrics; use SO pra entender que tipo de conteudo ressoa):\n"
            + dados_pbr + "\n"
        )
    else:
        bloco_dados = ""

    instrucao = (
        "Gere EXATAMENTE 3 ideias NOVAS de video (reels) para hoje ("
        + data_str + ").\n\n"
        "O FOCO #1 SAO OS TRENDS. Cada uma das 3 ideias deve PARTIR de um trend "
        "viral (um formato de reel em alta, um audio/musica do momento, um meme, "
        "um desafio, OU um dos assuntos em alta abaixo) e ADAPTAR pro cenario da "
        "PBR Brazil, usando um competidor de sucesso como personagem.\n\n"
        + bloco_trends
        + bloco_ranking
        + bloco_dados
        + "\nLEMBRETE CRITICO: os atletas NAO estao disponiveis pra gravar - NADA "
        "de dancinha/coreografia/atleta participando do trend. Use so material de "
        "arquivo (montarias, cortes), edicao, audio em alta por cima de clipes, "
        "texto na tela, comparacoes e narracao.\n\n"
        "REGRA: as ideias NAO podem repetir nem ser variacoes obvias de nenhuma "
        "ideia ja enviada. Historico completo (nao repita nada parecido):\n"
        "--- HISTORICO ---\n"
        + (historico or "(vazio - nenhuma ideia enviada ainda)")
        + "\n--- FIM DO HISTORICO ---\n\n"
        "Cada ideia precisa deixar claro: QUAL e o trend, e COMO adaptar pra PBR "
        "(formato, gancho dos primeiros 3s, e qual competidor/angulo usar).\n\n"
        "Use a ferramenta 'enviar_ideias' para responder com a mensagem de WhatsApp "
        "pronta e o resumo das 3 ideias."
    )

    ferramenta = {
        "name": "enviar_ideias",
        "description": "Envia a mensagem de WhatsApp pronta e o resumo das 3 ideias.",
        "input_schema": {
            "type": "object",
            "properties": {
                "whatsapp": {
                    "type": "string",
                    "description": (
                        "Mensagem pronta pra WhatsApp, em portugues com acentos e "
                        "POUCOS emojis (so um touro no titulo). Comece com a linha de "
                        "titulo: PBR Brazil - Trends de hoje (" + data_str + "). "
                        "Liste as 3 ideias numeradas 1) 2) 3). Cada ideia deve mostrar "
                        "em 2-3 linhas curtas: o TREND (qual e), como adaptar pra PBR "
                        "(formato + gancho dos primeiros 3s) e qual competidor/angulo. "
                        "Seja direta e pratica."
                    ),
                },
                "historico": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "As 3 ideias resumidas em 1 linha cada (Titulo - resumo).",
                },
            },
            "required": ["whatsapp", "historico"],
        },
    }

    resp = client.messages.create(
        model=MODELO,
        max_tokens=1500,
        system=sistema,
        tools=[ferramenta],
        tool_choice={"type": "tool", "name": "enviar_ideias"},
        messages=[{"role": "user", "content": instrucao}],
    )

    for bloco in resp.content:
        if getattr(bloco, "type", None) == "tool_use":
            return bloco.input
    raise RuntimeError("A IA nao retornou as ideias no formato esperado.")


def _dividir_mensagem(texto, limite=500):
    """Quebra a mensagem em partes (o CallMeBot trunca mensagens longas).

    Mantem blocos (ideias separadas por linha em branco) inteiros.
    """
    if len(texto) <= limite:
        return [texto]
    blocos = texto.split("\n\n")
    partes, atual = [], ""
    for bloco in blocos:
        candidato = (atual + "\n\n" + bloco) if atual else bloco
        if len(candidato) > limite and atual:
            partes.append(atual)
            atual = bloco
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
            parte = "(%d/%d)\n%s" % (i, total, parte)
        _enviar_parte(parte)
        if i < total:
            time.sleep(8)  # respeita o limite de frequencia do CallMeBot


def salvar_historico(linhas, data_str):
    bloco = "\n## " + data_str + "\n" + "".join(
        "%d. %s\n" % (i, linha) for i, linha in enumerate(linhas, 1)
    )
    with open(HISTORICO, "a", encoding="utf-8") as f:
        f.write(bloco)


def main():
    hoje = datetime.now(FUSO_BRASILIA)
    data_iso = hoje.strftime("%Y-%m-%d")
    data_br = hoje.strftime("%d/%m")

    historico = ler_historico()
    trends = puxar_trends()
    dados_pbr = puxar_dados_pbr()
    ranking = puxar_ranking_pbr()
    dados = gerar_ideias(historico, dados_pbr, ranking, trends, data_br)

    enviar_whatsapp(dados["whatsapp"])
    salvar_historico(dados["historico"], data_iso)
    print("OK - ideias enviadas e historico atualizado.")


if __name__ == "__main__":
    main()
