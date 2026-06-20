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

import json
import os
import sys
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


def gerar_ideias(historico, dados_pbr, data_str):
    """Pede a IA 3 ideias novas. Retorna dict com 'whatsapp' e 'historico'."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    sistema = (
        "Voce e um estrategista de conteudo para o Instagram da PBR Brazil "
        "(montaria em touros / rodeio profissional no Brasil). Seu trabalho e "
        "sugerir ideias de video (reels) que tendem a viralizar em 2026: gancho "
        "forte nos primeiros 2 segundos, estrutura situacao-tensao-resolucao, "
        "bastidores autenticos com ritmo, e formatos que geram salvamento e "
        "compartilhamento. Use linguagem de quem entende do nicho (peao, touro, "
        "arena, portao, montaria, evento). IMPORTANTE: escreva sempre em "
        "portugues do Brasil com acentuacao correta e use emojis quando fizer "
        "sentido (ex: o emoji de touro no inicio da mensagem)."
    )

    if dados_pbr:
        bloco_dados = (
            "DADOS REAIS de performance dos posts recentes da PBR Brazil "
            "(JSON do Supermetrics; analise quais temas/formatos tiveram mais "
            "curtidas, comentarios, alcance, salvamentos e compartilhamentos, e "
            "baseie as ideias no que esta funcionando):\n" + dados_pbr + "\n"
        )
    else:
        bloco_dados = (
            "OBS: nao foi possivel ler os dados internos da PBR hoje - baseie as "
            "ideias no seu conhecimento do nicho e em trends atuais de reels.\n"
        )

    instrucao = (
        "Gere EXATAMENTE 3 ideias NOVAS de video para hoje (" + data_str + ").\n\n"
        + bloco_dados
        + "\nREGRA CRITICA: as ideias NAO podem repetir nem ser variacoes obvias de "
        "nenhuma ideia ja enviada. Historico completo (nao repita nada parecido):\n\n"
        "--- HISTORICO ---\n"
        + (historico or "(vazio - nenhuma ideia enviada ainda)")
        + "\n--- FIM DO HISTORICO ---\n\n"
        "Cada ideia precisa de: titulo curto e chamativo, formato (ex: Reel 9-20s, "
        "POV, carrossel, mini vlog), gancho (os primeiros 3 segundos) e o porque "
        "(qual dado da PBR ou trend justifica).\n\n"
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
                        "Mensagem completa pronta pra enviar no WhatsApp, em portugues "
                        "com acentos corretos e emojis, curta e escaneavel. Comece com "
                        "uma linha de titulo tipo: (emoji de touro) PBR Brazil - Ideias "
                        "de video de hoje (" + data_str + "). Liste as 3 ideias numeradas "
                        "com titulo, formato, gancho, e termine com uma linha 'Base: ...'."
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


def enviar_whatsapp(mensagem):
    resp = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": PHONE, "apikey": CALLMEBOT_APIKEY, "text": mensagem},
        timeout=60,
    )
    print("Resposta CallMeBot:", resp.text[:300])
    if "queued" not in resp.text.lower() and "sent" not in resp.text.lower():
        sys.exit("ERRO: CallMeBot nao confirmou o envio.")


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
    dados_pbr = puxar_dados_pbr()
    dados = gerar_ideias(historico, dados_pbr, data_br)

    enviar_whatsapp(dados["whatsapp"])
    salvar_historico(dados["historico"], data_iso)
    print("OK - ideias enviadas e historico atualizado.")


if __name__ == "__main__":
    main()
