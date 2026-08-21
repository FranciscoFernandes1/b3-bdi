"""Coleta dos dados da B3.

Duas estratégias:

1. `via_api`   -> requisição HTTP direta ao endpoint interno. Rápido, sem
                 dependência de navegador. Exige conhecer o endpoint.
2. `via_browser` -> abre a página em um Chromium headless e intercepta as
                 respostas XHR. Mais lento (~5-10s/dia) mas funciona sem
                 conhecer o endpoint e sobrevive a mudanças de rota.

`coletar()` tenta (1) e cai para (2) automaticamente.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any

import requests

from . import config as cfg

log = logging.getLogger(__name__)


class SemDados(Exception):
    """A data existe mas não há tabela publicada (feriado, pregão sem BDI)."""


def _url_pagina(dia: date) -> str:
    return cfg.PAGE_URL.format(
        date=dia.strftime(cfg.DATE_FMT_PAGE),
        classification=cfg.CLASSIFICATION,
        table=cfg.TABLE,
        lang=cfg.LANG,
    )


# ---------------------------------------------------------------------------
# Estratégia 1: API direta
# ---------------------------------------------------------------------------
class MetodoErrado(Exception):
    """405: a URL existe, mas o verbo HTTP está errado."""


def _requisitar(url: str, metodo: str | None = None,
                corpo: str | None = None) -> Any:
    metodo = (metodo or cfg.API_METHOD).upper()
    corpo = cfg.API_BODY if corpo is None else corpo

    kwargs: dict[str, Any] = {"headers": dict(cfg.HEADERS), "timeout": cfg.TIMEOUT}
    if corpo:
        kwargs["data"] = corpo.encode("utf-8")
        kwargs["headers"]["Content-Type"] = "application/json"

    ultimo_erro: Exception | None = None
    for tentativa in range(1, cfg.RETRIES + 1):
        try:
            r = requests.request(metodo, url, **kwargs)

            if r.status_code == 404:
                raise SemDados(f"404 em {url}")
            if r.status_code == 405:
                raise MetodoErrado(f"{metodo} não é aceito em {url}")
            # 4xx é determinístico: repetir não muda o resultado e só gasta
            # tempo. Só 408 (timeout) e 429 (rate limit) merecem nova tentativa.
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                raise RuntimeError(f"HTTP {r.status_code} em {url}")

            r.raise_for_status()
            return r.json()

        except (SemDados, MetodoErrado):
            raise
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            log.warning("tentativa %d/%d falhou: %s", tentativa, cfg.RETRIES, e)
            if tentativa < cfg.RETRIES:
                time.sleep(cfg.BACKOFF * tentativa)

    raise RuntimeError(f"falha ao coletar {url}") from ultimo_erro


def _requisitar_com_fallback(url: str) -> Any:
    """Tenta o verbo configurado; em 405, tenta POST com corpo vazio."""
    try:
        return _requisitar(url)
    except MetodoErrado:
        if cfg.API_METHOD == "POST":
            raise
        log.warning("GET recusado (405); tentando POST")
        return _requisitar(url, metodo="POST", corpo="{}")


def via_api(de: date, ate: date, pagina: int = 1,
            tamanho: int | None = None) -> Any:
    """Uma página do intervalo [de, ate]."""
    if not cfg.API_URL:
        raise RuntimeError("B3_API_URL não configurada")
    return _requisitar_com_fallback(cfg.API_URL.format(
        table=cfg.TABLE,
        de=de.strftime(cfg.DATE_FMT_API),
        ate=ate.strftime(cfg.DATE_FMT_API),
        pagina=pagina,
        tamanho=tamanho or cfg.PAGE_SIZE,
        classification=cfg.CLASSIFICATION,
        lang=cfg.LANG,
        date=de.strftime(cfg.DATE_FMT_API),
    ))


def coletar_intervalo(de: date, ate: date) -> list[Any]:
    """Todas as páginas do intervalo. Devolve uma lista de payloads."""
    from .parse import localizar_valores

    tamanho = cfg.PAGE_SIZE
    paginas: list[Any] = []

    for n in range(1, cfg.MAX_PAGINAS + 1):
        payload = via_api(de, ate, pagina=n, tamanho=tamanho)
        linhas = len(localizar_valores(payload))
        if linhas == 0:
            break
        paginas.append(payload)
        log.info("  página %d: %d linhas", n, linhas)
        if linhas < tamanho:
            break  # página incompleta = última
    else:
        log.warning("limite de %d páginas atingido — pode haver dados truncados",
                    cfg.MAX_PAGINAS)

    return paginas


# ---------------------------------------------------------------------------
# Estratégia 2: navegador headless com interceptação de rede
# ---------------------------------------------------------------------------
def via_browser(dia: date, *, retornar_urls: bool = False) -> Any:
    from playwright.sync_api import sync_playwright

    capturado: list[tuple] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=cfg.HEADERS["User-Agent"],
            locale="pt-BR",
        )
        page = ctx.new_page()

        def on_response(resp):
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            if "arquivos.b3.com.br" not in resp.url:
                return
            try:
                dados = resp.json()
            except Exception:  # noqa: BLE001
                return
            req = resp.request
            capturado.append((resp.url, dados, {
                "metodo": req.method,
                "corpo": req.post_data,
                "content_type": req.headers.get("content-type"),
            }))

        page.on("response", on_response)
        page.goto(_url_pagina(dia), wait_until="domcontentloaded",
                  timeout=cfg.TIMEOUT * 1000)

        # Espera ATIVA pela resposta da tabela. `networkidle` + pausa fixa é
        # frágil: numa execução a tabela chega em 1s, na seguinte em 6s e a
        # captura sai incompleta sem qualquer erro.
        limite = cfg.ESPERA_TABELA * 1000
        passo, esperou = 500, 0
        while esperou < limite:
            if any(cfg.TABLE.lower() in c[0].lower() for c in capturado):
                page.wait_for_timeout(1000)  # deixa a paginação terminar
                break
            page.wait_for_timeout(passo)
            esperou += passo
        else:
            log.warning("tabela %s não respondeu em %ds", cfg.TABLE,
                        cfg.ESPERA_TABELA)
        browser.close()

    if retornar_urls:
        return capturado

    if not capturado:
        raise SemDados(f"nenhum payload JSON capturado para {dia}")

    # A resposta certa é a que traz o nome da tabela na URL. Escolher pelo
    # maior payload pegaria `classifications` (o catálogo do site, ~67 KB),
    # que é 5x maior que os dados e não contém linha nenhuma.
    da_tabela = [c for c in capturado if cfg.TABLE.lower() in c[0].lower()]
    if not da_tabela:
        raise SemDados(f"nenhuma resposta de {cfg.TABLE} para {dia}")
    _, payload = max(da_tabela, key=lambda t: len(json.dumps(t[1])))
    return payload


# ---------------------------------------------------------------------------
def coletar(de: date, ate: date) -> list[Any]:
    """Coleta o intervalo, caindo para o navegador se a API direta falhar."""
    if cfg.API_URL:
        try:
            return coletar_intervalo(de, ate)
        except SemDados:
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("API direta falhou (%s); usando navegador", e)
    # O fallback de navegador só sabe carregar um dia por vez.
    return [via_browser(ate)]
