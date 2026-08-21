"""Normalização do payload da B3 em DataFrame.

A API do BDI devolve a tabela em formato colunar compacto: um array de
definições de coluna (com nome amigável, tipo e formato) e um array de valores
como lista de listas. Algo como:

    {"columns": [{"name": "DateRef", "friendlyNamePt": "Data de referência",
                  "format": "DD/MM/YYYY", ...}, ...],
     "values":  [["31/07/2026", "DI1", "F27", "1.234.567", "99.876,54"], ...]}

Os nomes de coluna saem do `friendlyNamePt`, então a tabela final já vem com
cabeçalhos legíveis em português em vez de identificadores internos.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any

import pandas as pd

from . import config as cfg

# Chaves que denunciam uma definição de coluna (e não uma linha de dado)
_MARCAS_COLUNA = {
    "friendlyName", "friendlyNamePt", "friendlyNameEn",
    "columnAlignment", "valueAlignment", "isVisible", "hideColumn",
}


def _slug(texto: str) -> str:
    """'Preço de Ajuste (R$)' -> 'preco_de_ajuste_rs'"""
    t = unicodedata.normalize("NFKD", str(texto))
    t = t.encode("ascii", "ignore").decode()
    t = re.sub(r"[^\w\s]", " ", t).strip().lower()
    t = re.sub(r"\s+", "_", t)
    return re.sub(r"_+", "_", t).strip("_")


# ---------------------------------------------------------------------------
# Localização das partes do payload
# ---------------------------------------------------------------------------
def _percorrer(no: Any):
    """Gera todas as listas encontradas no payload, em profundidade."""
    if isinstance(no, list):
        yield no
        for item in no:
            yield from _percorrer(item)
    elif isinstance(no, dict):
        for v in no.values():
            yield from _percorrer(v)


def localizar_colunas(payload: Any) -> list[dict]:
    """Maior lista de dicionários que se parecem com definições de coluna."""
    melhor: list[dict] = []
    for lista in _percorrer(payload):
        if not lista or not all(isinstance(i, dict) for i in lista):
            continue
        if not (_MARCAS_COLUNA & set(lista[0])):
            continue
        if len(lista) > len(melhor):
            melhor = lista
    return melhor


def localizar_valores(payload: Any) -> list[list]:
    """Maior lista cujos itens são listas — o bloco de dados."""
    melhor: list[list] = []
    for lista in _percorrer(payload):
        if not lista or not all(isinstance(i, list) for i in lista):
            continue
        if len(lista) > len(melhor):
            melhor = lista
    return melhor


def localizar_registros(payload: Any) -> list[dict]:
    """Fallback: maior lista de dicionários que NÃO sejam definições de coluna."""
    melhor: list[dict] = []
    for lista in _percorrer(payload):
        if not lista or not all(isinstance(i, dict) for i in lista):
            continue
        if _MARCAS_COLUNA & set(lista[0]):
            continue
        if len(lista) > len(melhor):
            melhor = lista
    return melhor


def nomes_de_coluna(colunas: list[dict]) -> list[str]:
    nomes: list[str] = []
    vistos: dict[str, int] = {}
    for c in colunas:
        bruto = (c.get("friendlyNamePt") or c.get("friendlyName")
                 or c.get("name") or "")
        nome = _slug(bruto) or f"col_{len(nomes)}"
        if nome in vistos:  # desambigua colisões geradas pelo slug
            vistos[nome] += 1
            nome = f"{nome}_{vistos[nome]}"
        else:
            vistos[nome] = 0
        nomes.append(nome)
    return nomes


# ---------------------------------------------------------------------------
# Conversão de tipos
# ---------------------------------------------------------------------------
_RE_NUM = re.compile(
    r"^[+-]?\s*(?:R\$\s*)?"
    r"(?:\d{1,3}(?:\.\d{3})+|\d+)"
    r"(?:,\d+)?"
    r"\s*%?$"
)
_VAZIOS = {"", "-", "--", "n/a", "na", "nd", "n.d."}
_RE_DATA_BR = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_RE_DATA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")

# Códigos de `type` observados nos metadados da B3. Usados só como pista: se a
# conversão indicada falhar, o heurístico assume.
TIPO_DATA = 4


def _parse_data_escalar(valor: Any):
    """Aceita ISO 8601 e DD/MM/AAAA. Devolve date ou None."""
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    try:
        return datetime.fromisoformat(texto).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(texto, "%d/%m/%Y").date()
    except ValueError:
        return None


def _limpar_numero(s: pd.Series) -> pd.Series:
    """Converte '1.234.567,89' -> 1234567.89.

    Só converte a coluna inteira se TODOS os valores preenchidos forem números
    no formato brasileiro. Uma única célula como 'F27' aborta a conversão e a
    coluna permanece texto — é preferível a corromper códigos de vencimento.
    """
    if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
        return s

    texto = s.astype("string").fillna("").str.strip()
    preenchidos = texto[~texto.str.lower().isin(_VAZIOS)]
    if preenchidos.empty or not preenchidos.str.match(_RE_NUM).all():
        return s

    limpa = (
        texto.str.replace(r"[R$%\s]", "", regex=True)
        .str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    return pd.to_numeric(limpa, errors="coerce")


def _limpar_data(s: pd.Series) -> pd.Series | None:
    """Converte a coluna para datetime se TODOS os valores forem datas.

    Aceita os dois formatos que a B3 usa: ISO 8601 nos valores
    ('2026-08-19T00:00:00') e DD/MM/AAAA. O campo `format` dos metadados diz
    'DD/MM/YYYY', mas isso é o formato de EXIBIÇÃO da interface — os valores
    chegam em ISO. Confiar no `format` para fazer o parse quebra tudo.
    """
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    texto = s.astype("string").fillna("").str.strip()
    preenchidos = texto[~texto.str.lower().isin(_VAZIOS)]
    if preenchidos.empty:
        return None

    if preenchidos.str.match(_RE_DATA_ISO).all():
        return pd.to_datetime(texto, format="ISO8601", errors="coerce")
    if preenchidos.str.match(_RE_DATA_BR).all():
        return pd.to_datetime(texto, format="%d/%m/%Y", errors="coerce")
    return None


# ---------------------------------------------------------------------------
def normalizar(payload: Any, dia: date | None = None) -> pd.DataFrame:
    """Converte um payload da B3 em DataFrame com `data_referencia`.

    `dia` só é usado como fallback quando o payload não traz coluna de data.
    """
    colunas = localizar_colunas(payload)
    valores = localizar_valores(payload)

    if colunas and valores:
        nomes = nomes_de_coluna(colunas)
        largura = max(len(v) for v in valores)
        linhas = [list(v) + [None] * (largura - len(v)) for v in valores]

        # A B3 devolve uma posição a mais do que declara em `columns` — no
        # AnalyticalFramework2 são 5 colunas para 6 valores, com a última
        # sempre nula. Extras 100% vazias são descartadas; extras com
        # conteúdo ganham nome genérico para não perder dado silenciosamente.
        while largura > len(nomes):
            idx = largura - 1
            if all(l[idx] in (None, "") for l in linhas):
                for l in linhas:
                    del l[idx]
                largura -= 1
            else:
                nomes += [f"col_{i}" for i in range(len(nomes), largura)]
                break

        df = pd.DataFrame(linhas, columns=nomes[:largura])
    else:
        registros = localizar_registros(payload)
        if not registros:
            return pd.DataFrame()
        df = pd.json_normalize(registros, sep="_")
        df.columns = [_slug(c) for c in df.columns]

    if df.empty:
        return df

    # Datas primeiro: '31/07/2026' não casa com _RE_NUM, mas a ordem deixa
    # explícito que a coluna de data é resolvida antes de tudo.
    col_data = None
    for col in df.columns:
        convertida = _limpar_data(df[col])
        if convertida is not None:
            df[col] = convertida
            if col_data is None:
                col_data = col

    for col in df.columns:
        if not pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = _limpar_numero(df[col])

    # Partição: preferir a data que veio no próprio dado
    if col_data is not None:
        df.insert(0, cfg.COL_DATA, df[col_data].dt.normalize())
        df = df.drop(columns=[col_data])
    elif dia is not None:
        df.insert(0, cfg.COL_DATA, pd.Timestamp(dia).normalize())
    else:
        raise ValueError("payload sem coluna de data e nenhum `dia` informado")

    return df


def resumo_colunas(payload: Any) -> pd.DataFrame:
    """Metadados de coluna — útil para documentar o schema real da tabela."""
    colunas = localizar_colunas(payload)
    if not colunas:
        return pd.DataFrame()
    campos = ["name", "friendlyNamePt", "friendlyNameEn", "type",
              "format", "isVisible", "order"]
    return pd.DataFrame([{c: col.get(c) for c in campos} for col in colunas])


def dividir_por_data(payload: Any) -> dict[date, dict]:
    """Quebra um payload de intervalo em um payload por data de referência.

    Permite guardar em raw/ um arquivo por pregão mesmo tendo baixado a janela
    inteira numa requisição só. Sem isso, cada execução gravaria um snapshot
    de 21 dias e o raw/ acumularia 21x de redundância.
    """
    colunas = localizar_colunas(payload)
    valores = localizar_valores(payload)
    if not (colunas and valores):
        return {}

    # Acha a coluna de data pelo conteúdo, não pelos metadados: `format` é o
    # formato de exibição e não descreve o valor que chega.
    idx = next(
        (i for i in range(min(len(colunas), len(valores[0])))
         if _parse_data_escalar(valores[0][i]) is not None),
        None,
    )
    if idx is None:
        idx = next((i for i, c in enumerate(colunas)
                    if c.get("type") == TIPO_DATA), None)
    if idx is None:
        return {}

    grupos: dict[date, list[list]] = {}
    for linha in valores:
        if idx >= len(linha):
            continue
        dia = _parse_data_escalar(linha[idx])
        if dia is None:
            continue
        grupos.setdefault(dia, []).append(linha)

    return {d: {"columns": colunas, "values": v} for d, v in grupos.items()}


def limites_da_tabela(payload: Any) -> dict:
    """Lê os limites que a própria B3 declara no payload.

    `hasHistory` e `limitDate` dizem, sem precisar sondar, se a tabela guarda
    série histórica e até quantos dias para trás ela responde.
    """
    def achar(no: Any) -> dict | None:
        if isinstance(no, dict):
            if "hasHistory" in no or "limitDate" in no:
                return no
            for v in no.values():
                r = achar(v)
                if r:
                    return r
        elif isinstance(no, list):
            for i in no:
                r = achar(i)
                if r:
                    return r
        return None

    tabela = achar(payload) or {}
    return {
        "nome": tabela.get("friendlyNamePt") or tabela.get("name"),
        "hasHistory": tabela.get("hasHistory"),
        "limitDate": tabela.get("limitDate"),
        "pageCount": tabela.get("pageCount"),
    }
