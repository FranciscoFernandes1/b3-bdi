"""Configuração central do pipeline BDI/B3."""
from __future__ import annotations

import os
from pathlib import Path


def _env(nome: str, padrao: str) -> str:
    """os.getenv que trata string vazia como ausente.

    No GitHub Actions, `${{ vars.X }}` de uma variável não definida vira string
    vazia em vez de sumir do ambiente. Com os.getenv(nome, padrao) puro, isso
    sobrescreve o padrão por "" e o pipeline degrada em silêncio.
    """
    valor = os.getenv(nome)
    return valor if valor not in (None, "") else padrao

# --------------------------------------------------------------------------
# Identificação da tabela na B3
# --------------------------------------------------------------------------
# Estes valores vêm da querystring da página que você abre no navegador:
# https://arquivos.b3.com.br/bdi/tabelas/31-07-2026
#     ?classification=15e17436-a160-495a-a159-bdffcc76dfa3
#     &table=AnalyticalFramework2
#     &lang=pt-BR
CLASSIFICATION = _env(
    "B3_CLASSIFICATION", "15e17436-a160-495a-a159-bdffcc76dfa3"
)
TABLE = _env("B3_TABLE", "AnalyticalFramework2")
LANG = _env("B3_LANG", "pt-BR")

# Nome curto usado nos arquivos de saída
DATASET = _env("B3_DATASET", "bdi_analytical_framework2")

# --------------------------------------------------------------------------
# Endpoint da API interna
# --------------------------------------------------------------------------
# Descoberto via `python run.py descobrir`. Estrutura do path:
#   /bdi/table/{tabela}/{data_inicial}/{data_final}/{pagina}/{itens_por_pagina}
#
# ATENÇÃO: o segundo segmento NÃO é fim de intervalo. Pedir 30/07 a 20/08
# devolve só 30/07. É um limite (o mesmo conceito do `limitDate: "D-21"` que
# a tabela declara), e o próprio site sempre chama com as duas datas iguais.
# Por isso a coleta é dia a dia.
API_URL = _env(
    "B3_API_URL",
    "https://arquivos.b3.com.br/bdi/table/{table}/{de}/{ate}/{pagina}/{tamanho}",
)

# Verbo HTTP e corpo da requisição. O `descobrir` informa os valores corretos.
# Vazio em API_BODY = requisição sem corpo.
API_METHOD = _env("B3_API_METHOD", "POST").upper()  # confirmado pelo descobrir
API_BODY = _env("B3_API_BODY", "{}")               # corpo vazio, mas obrigatório

# Itens por página. A página do site usa 100; valores maiores reduzem o número
# de requisições, mas o servidor pode impor um teto — a paginação cobre o caso.
PAGE_SIZE = int(_env("B3_PAGE_SIZE", "1000"))
MAX_PAGINAS = 200  # trava de segurança contra laço infinito

PAGE_URL = (
    "https://arquivos.b3.com.br/bdi/tabelas/{date}"
    "?classification={classification}&table={table}&lang={lang}"
)

DATE_FMT_PAGE = "%d-%m-%Y"   # formato usado na URL da página
DATE_FMT_API = "%Y-%m-%d"    # confirmado: AAAA-MM-DD no path da API
DATE_FMT_FILE = "%Y-%m-%d"   # formato usado nos nomes de arquivo (não mude)

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://arquivos.b3.com.br/",
}
TIMEOUT = 45
# Pausa entre requisições. A B3 parece devolver 200 com corpo vazio quando
# recebe rajadas — o que é indistinguível de "não publicou". Espaçar é o que
# separa uma série completa de uma com buracos silenciosos.
PAUSA = float(_env("B3_PAUSA", "1.5"))

# Uma resposta vazia pode ser pregão sem publicação OU limitação de taxa.
# Antes de aceitar o vazio como definitivo, tentamos de novo após uma pausa
# maior. Se vier vazio duas vezes, aí sim é ausência real.
RECONFERIR_VAZIO = _env("B3_RECONFERIR_VAZIO", "1") == "1"
PAUSA_RECONFERIR = float(_env("B3_PAUSA_RECONFERIR", "6"))

ESPERA_TABELA = 25  # segundos que o navegador aguarda a tabela responder
RETRIES = 3
BACKOFF = 4  # segundos, multiplicado pela tentativa

# --------------------------------------------------------------------------
# Janela de sincronização
# --------------------------------------------------------------------------
# O site mantém ~15 dias. Varremos uma janela um pouco maior em dias corridos
# para absorver feriados e eventuais atrasos de publicação.
JANELA_DIAS = int(_env("B3_JANELA_DIAS", "21"))

# --------------------------------------------------------------------------
# Caminhos
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
DIR_RAW = ROOT / "dados" / "raw"
DIR_CURATED = ROOT / "dados" / "curated"

PARQUET = DIR_CURATED / f"{DATASET}.parquet"
CSV = DIR_CURATED / f"{DATASET}.csv"

# Coluna de partição criada pelo pipeline (não vem da B3)
COL_DATA = "data_referencia"
