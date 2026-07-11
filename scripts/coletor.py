#!/usr/bin/env python3
"""
Coletor do Observatório Clarim.

Lê config.json, percorre os grupos e sites cadastrados, coleta notícias novas
(via RSS, sitemap.xml ou uma URL manual passada por argumento), gera um resumo
com o Gemini, e grava/atualiza data/<grupo>.json sem apagar o histórico.

Modos de site suportados nesta fase (MVP):
  - "rss":     feed RSS/Atom padrão.
  - "sitemap": sitemap.xml/news-sitemap.xml; usado quando o site não tem RSS.
  - "manual":  não roda sozinho; só processa quando --url é passado (disparado
               pela tela de admin via workflow_dispatch).

O modo "scraping" (aprendizado por seleção visual) fica para a Fase 2.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

RAIZ = Path(__file__).resolve().parent.parent
CONFIG_PATH = RAIZ / "config.json"
DATA_DIR = RAIZ / "data"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ObservatorioClarimBot/1.0; "
        "+https://github.com/)"
    )
}

DIAS_PARA_ALERTA = 30
DIAS_PARA_ARQUIVAR = 30


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def agora_iso():
    return datetime.now(timezone.utc).isoformat()


def gerar_id(link: str) -> str:
    return hashlib.sha256(link.strip().encode("utf-8")).hexdigest()[:16]


def carregar_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def salvar_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")


def caminho_dados(grupo_id: str) -> Path:
    return DATA_DIR / f"{grupo_id}.json"


def carregar_dados(grupo_id: str):
    caminho = caminho_dados(grupo_id)
    if not caminho.exists():
        return []
    with open(caminho, "r", encoding="utf-8") as f:
        return json.load(f)


def salvar_dados(grupo_id: str, itens):
    caminho = caminho_dados(grupo_id)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(itens, f, ensure_ascii=False, indent=2)
        f.write("\n")


def links_conhecidos(itens):
    return {item["link"] for item in itens}


def mes_da_data(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m")


def caminho_arquivo_mes(grupo_id: str, mes: str) -> Path:
    return DATA_DIR / grupo_id / f"{mes}.json"


def carregar_arquivo_mes(grupo_id: str, mes: str):
    caminho = caminho_arquivo_mes(grupo_id, mes)
    if not caminho.exists():
        return []
    with open(caminho, "r", encoding="utf-8") as f:
        return json.load(f)


def salvar_arquivo_mes(grupo_id: str, mes: str, itens):
    caminho = caminho_arquivo_mes(grupo_id, mes)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(itens, f, ensure_ascii=False, indent=2)
        f.write("\n")


def arquivar_antigos(grupo_id: str, itens, grupo_cfg):
    """Separa os itens com DIAS_PARA_ARQUIVAR+ dias no feed (contados da captura)
    e os move para data/<grupo>/<AAAA-MM>.json, agrupados pelo mês de publicação.
    Retorna só os itens que continuam no feed atual."""
    atuais = []
    novos_por_mes = {}

    for item in itens:
        dias_no_feed = (datetime.now(timezone.utc) - datetime.fromisoformat(item["data_captura"])).days
        if dias_no_feed >= DIAS_PARA_ARQUIVAR:
            mes = mes_da_data(item["data_publicacao"])
            novos_por_mes.setdefault(mes, []).append(item)
        else:
            atuais.append(item)

    meses_tocados = set(grupo_cfg.get("arquivo_meses", []))
    for mes, itens_novos in novos_por_mes.items():
        existentes = carregar_arquivo_mes(grupo_id, mes)
        links_existentes = links_conhecidos(existentes)
        for item in itens_novos:
            if item["link"] not in links_existentes:
                existentes.append(item)
                links_existentes.add(item["link"])
        existentes.sort(key=lambda i: i["data_publicacao"], reverse=True)
        salvar_arquivo_mes(grupo_id, mes, existentes)
        meses_tocados.add(mes)

    grupo_cfg["arquivo_meses"] = sorted(meses_tocados)
    return atuais


# ---------------------------------------------------------------------------
# Geração de resumo (Gemini / Google AI Studio)
# ---------------------------------------------------------------------------

def gerar_resumo(titulo: str, texto_base: str) -> str:
    """Gera um resumo de até 3 parágrafos via Gemini. Cai para o texto
    original (truncado) se a chave não estiver configurada ou a chamada falhar."""
    api_key = os.environ.get("GEMINI_API_KEY")
    texto_base = (texto_base or "").strip()

    if not api_key:
        return texto_base[:600]

    prompt = (
        "Você é um assistente de curadoria de notícias. Escreva um resumo em "
        "português do Brasil para a notícia abaixo. Use 2 parágrafos; se a "
        "notícia for muito densa, pode usar até 3 parágrafos. Seja objetivo, "
        "sem opinião, sem repetir o título literalmente na primeira frase.\n\n"
        f"Título: {titulo}\n\nConteúdo/descrição disponível:\n{texto_base[:4000]}"
    )

    try:
        resposta = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.0-flash:generateContent",
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        resposta.raise_for_status()
        dados = resposta.json()
        return dados["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as erro:  # noqa: BLE001
        print(f"  [aviso] Gemini falhou ({erro}); usando texto original.", file=sys.stderr)
        return texto_base[:600]


# ---------------------------------------------------------------------------
# Extração de Open Graph (usado por sitemap e manual)
# ---------------------------------------------------------------------------

def extrair_open_graph(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    sopa = BeautifulSoup(resp.text, "html.parser")

    def meta(prop):
        tag = sopa.find("meta", property=prop) or sopa.find("meta", attrs={"name": prop})
        return tag["content"].strip() if tag and tag.get("content") else None

    titulo = meta("og:title") or (sopa.title.string.strip() if sopa.title else url)
    imagem = meta("og:image")
    descricao = meta("og:description") or meta("description") or ""

    return {"titulo": titulo, "imagem": imagem, "descricao": descricao}


# ---------------------------------------------------------------------------
# Coleta por modo
# ---------------------------------------------------------------------------

def extrair_thumbnail_rss(entrada) -> str:
    if "media_thumbnail" in entrada and entrada.media_thumbnail:
        return entrada.media_thumbnail[0].get("url", "")
    if "media_content" in entrada and entrada.media_content:
        return entrada.media_content[0].get("url", "")
    for link in entrada.get("links", []):
        if str(link.get("type", "")).startswith("image/"):
            return link.get("href", "")

    # tenta achar <img> no resumo e, se não achar, no conteúdo completo
    # (content:encoded costuma trazer o corpo inteiro do post, com a imagem)
    candidatos_html = [entrada.get("summary", "") or entrada.get("description", "")]
    for bloco in entrada.get("content", []):
        candidatos_html.append(bloco.get("value", ""))

    for html_bruto in candidatos_html:
        match = re.search(r'<img[^>]+src="([^"]+)"', html_bruto)
        if match:
            return match.group(1)
    return ""


def coletar_rss(site, ja_conhecidos):
    novos = []
    feed = feedparser.parse(site["url_feed"], request_headers=HEADERS)

    if feed.bozo and not feed.entries:
        raise ValueError(
            f"a URL não parece ser um feed RSS/Atom válido "
            f"({site.get('url_feed')}) — confira se falta um caminho tipo /feed/ no final"
        )

    for entrada in feed.entries:
        link = entrada.get("link", "").strip()
        if not link or link in ja_conhecidos:
            continue
        titulo = entrada.get("title", "(sem título)")
        resumo_bruto = entrada.get("summary", "") or entrada.get("description", "")
        resumo_bruto = BeautifulSoup(resumo_bruto, "html.parser").get_text(" ", strip=True)
        thumbnail = extrair_thumbnail_rss(entrada)
        if not thumbnail:
            try:
                thumbnail = extrair_open_graph(link)["imagem"] or ""
            except Exception as erro:  # noqa: BLE001
                print(f"  [aviso] sem thumbnail via OG para {link}: {erro}", file=sys.stderr)
                thumbnail = ""
        if "published_parsed" in entrada and entrada.published_parsed:
            data_pub = datetime(*entrada.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        else:
            data_pub = agora_iso()

        novos.append({
            "link": link,
            "titulo": titulo,
            "thumbnail": thumbnail,
            "resumo": gerar_resumo(titulo, resumo_bruto),
            "data_publicacao": data_pub,
        })
    return novos


def coletar_sitemap(site, ja_conhecidos, limite=50):
    novos = []
    resp = requests.get(site["url_sitemap"], headers=HEADERS, timeout=20)
    resp.raise_for_status()
    sopa = BeautifulSoup(resp.text, "xml")
    locs = [loc.get_text(strip=True) for loc in sopa.find_all("loc")]

    candidatos = [u for u in locs if u not in ja_conhecidos][:limite]

    for url in candidatos:
        try:
            og = extrair_open_graph(url)
        except Exception as erro:  # noqa: BLE001
            print(f"  [aviso] falha ao ler {url}: {erro}", file=sys.stderr)
            continue
        novos.append({
            "link": url,
            "titulo": og["titulo"],
            "thumbnail": og["imagem"] or "",
            "resumo": gerar_resumo(og["titulo"], og["descricao"]),
            "data_publicacao": agora_iso(),
        })
    return novos


def coletar_manual(url):
    og = extrair_open_graph(url)
    return {
        "link": url,
        "titulo": og["titulo"],
        "thumbnail": og["imagem"] or "",
        "resumo": gerar_resumo(og["titulo"], og["descricao"]),
        "data_publicacao": agora_iso(),
    }


# ---------------------------------------------------------------------------
# Execução principal
# ---------------------------------------------------------------------------

def processar_grupo(grupo_id, grupo_cfg):
    itens = carregar_dados(grupo_id)
    ja_conhecidos = links_conhecidos(itens)
    houve_novidade_em = set()

    for site in grupo_cfg.get("sites", []):
        modo = site.get("modo")
        print(f"[{grupo_id}] {site['nome']} (modo={modo})")

        houve_erro = False
        mensagem_erro = ""
        try:
            if modo == "rss":
                novos = coletar_rss(site, ja_conhecidos)
            elif modo == "sitemap":
                novos = coletar_sitemap(site, ja_conhecidos)
            else:
                novos = []  # "manual" não roda na coleta diária
        except Exception as erro:  # noqa: BLE001
            print(f"  [erro] {site['nome']}: {erro}", file=sys.stderr)
            novos = []
            houve_erro = True
            mensagem_erro = str(erro)[:200]

        for novo in novos:
            novo["id"] = gerar_id(novo["link"])
            novo["fonte"] = site["nome"]
            novo["site_id"] = site["id"]
            novo["grupo"] = grupo_id
            novo["data_captura"] = agora_iso()
            itens.append(novo)
            ja_conhecidos.add(novo["link"])

        if houve_erro:
            site["alerta"] = True  # erro real: não espera os 30 dias pra avisar
            site["motivo_alerta"] = f"Erro na última coleta: {mensagem_erro}"
        elif novos:
            houve_novidade_em.add(site["id"])
            site["ultima_novidade"] = agora_iso()
            site["alerta"] = False
            site["motivo_alerta"] = None
        else:
            ultima = site.get("ultima_novidade")
            if ultima:
                dias = (datetime.now(timezone.utc) - datetime.fromisoformat(ultima)).days
                site["alerta"] = dias >= DIAS_PARA_ALERTA
                site["motivo_alerta"] = f"Sem novidades há {dias} dias" if site["alerta"] else None
        print(f"  -> {len(novos)} notícia(s) nova(s)")

    itens = arquivar_antigos(grupo_id, itens, grupo_cfg)
    salvar_dados(grupo_id, itens)


def processar_manual(config, grupo_id, url):
    if grupo_id not in config["grupos"]:
        raise SystemExit(f"Grupo '{grupo_id}' não existe no config.json")

    itens = carregar_dados(grupo_id)
    if url in links_conhecidos(itens):
        print("URL já registrada, nada a fazer.")
        return

    novo = coletar_manual(url)
    novo["id"] = gerar_id(novo["link"])
    novo["fonte"] = novo["titulo"].split(" - ")[-1] if " - " in novo["titulo"] else url.split("/")[2]
    novo["site_id"] = "manual"
    novo["grupo"] = grupo_id
    novo["data_captura"] = agora_iso()
    itens.append(novo)
    salvar_dados(grupo_id, itens)
    print(f"Notícia manual adicionada em '{grupo_id}': {novo['titulo']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--grupo", help="Grupo alvo (usado com --url para adição manual)")
    parser.add_argument("--url", help="URL de uma notícia específica para adicionar manualmente")
    args = parser.parse_args()

    config = carregar_config()

    if args.url:
        if not args.grupo:
            raise SystemExit("--url exige --grupo")
        processar_manual(config, args.grupo, args.url)
        salvar_config(config)
        return

    for grupo_id, grupo_cfg in config["grupos"].items():
        processar_grupo(grupo_id, grupo_cfg)

    salvar_config(config)
    print("Coleta concluída.")


if __name__ == "__main__":
    main()
