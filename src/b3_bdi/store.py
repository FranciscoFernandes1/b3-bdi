"""Persistência dos dados.

Duas camadas:

- raw/      snapshot imutável do JSON recebido, um arquivo por data.
            É a trilha de auditoria: se a normalização mudar, dá para
            reprocessar tudo sem voltar à B3 (que já terá perdido os dias).
- curated/  tabela consolidada, deduplicada.

O upsert é *delete-by-partition*: ao reprocessar uma data, todas as linhas
daquela data são removidas e substituídas pelas novas. É idempotente — rodar
o mesmo dia dez vezes produz exatamente o mesmo resultado.

Formato do curated
------------------
O CSV é sempre gravado — é o que o Power BI e os colegas consomem, e é o que
produz diff legível no git. Junto dele vai um `.schema.json` com os dtypes,
que torna o CSV totalmente round-trippable (senão Int64 volta como float e o
upsert incremental sofre com drift de tipo a cada execução).

O parquet é gravado **só se houver engine disponível**. Ele é mais rápido e
guarda os tipos nativamente, mas é dependência opcional: em Python novo sem
wheel de pyarrow, o pipeline segue funcionando pelo CSV.
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import date
from typing import Any

import pandas as pd

from . import config as cfg

log = logging.getLogger(__name__)


def _engine_parquet() -> str | None:
    for nome in ("pyarrow", "fastparquet"):
        try:
            __import__(nome)
            return nome
        except ImportError:
            continue
    return None


ENGINE = _engine_parquet()
SCHEMA = cfg.DIR_CURATED / f"{cfg.DATASET}.schema.json"
LACUNAS = cfg.DIR_CURATED / f"{cfg.DATASET}_lacunas.csv"


def salvar_raw(payload: Any, dia: date) -> None:
    destino = cfg.DIR_RAW / f"{dia:%Y}" / f"{dia:%m}"
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / f"{dia.strftime(cfg.DATE_FMT_FILE)}.json.gz"
    with gzip.open(caminho, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log.info("raw gravado: %s", caminho.relative_to(cfg.ROOT))


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------
def _ler_csv_tipado() -> pd.DataFrame:
    """Lê o CSV como texto e aplica o schema manualmente.

    Passar `dtype=` direto ao read_csv não funciona: o pandas aplica o dtype
    ANTES de honrar `decimal=","`, e "99876,54" levanta ValueError. Ler tudo
    como string e converter depois é determinístico e imune a essa ordem.
    """
    df = pd.read_csv(
        cfg.CSV,
        sep=";",
        encoding="utf-8-sig",
        dtype=str,
        keep_default_na=False,
        na_values=[""],
    )
    if not SCHEMA.exists():
        return df

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    for col, tipo in schema.items():
        if col not in df.columns:
            continue
        try:
            if tipo.startswith("datetime"):
                df[col] = pd.to_datetime(df[col], errors="coerce").astype(tipo)
            elif tipo[0].lower() in "if":  # Int64/int64/Float64/float64
                # to_csv grava com decimal="," e sem separador de milhar,
                # então basta trocar a vírgula decimal.
                limpa = df[col].str.replace(",", ".", regex=False)
                df[col] = pd.to_numeric(limpa, errors="coerce").astype(tipo)
            else:
                df[col] = df[col].astype(tipo)
        except (ValueError, TypeError) as e:
            log.warning("coluna %r: schema diz %s mas a conversão falhou (%s); "
                        "mantendo como texto", col, tipo, e)
    return df


def carregar_curated() -> pd.DataFrame:
    if ENGINE and cfg.PARQUET.exists():
        return pd.read_parquet(cfg.PARQUET, engine=ENGINE)
    if cfg.CSV.exists():
        return _ler_csv_tipado()
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Escrita
# ---------------------------------------------------------------------------
def _gravar(df: pd.DataFrame) -> None:
    cfg.DIR_CURATED.mkdir(parents=True, exist_ok=True)

    df.to_csv(
        cfg.CSV,
        index=False,
        encoding="utf-8-sig",
        sep=";",
        decimal=",",
        date_format="%Y-%m-%d",
    )
    SCHEMA.write_text(
        json.dumps({c: str(t) for c, t in df.dtypes.items()},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if ENGINE:
        df.to_parquet(cfg.PARQUET, index=False, engine=ENGINE,
                      compression="snappy")
    elif cfg.PARQUET.exists():
        # Engine sumiu do ambiente: o parquet viraria um arquivo obsoleto
        # silenciosamente divergente do CSV.
        cfg.PARQUET.unlink()
        log.warning("parquet removido — nenhuma engine disponível neste ambiente")


def upsert(novo: pd.DataFrame) -> tuple[int, int]:
    """Substitui as partições de data presentes em `novo`.

    Retorna (linhas_antes, linhas_depois).
    """
    atual = carregar_curated()
    antes = len(atual)

    if novo.empty:
        return antes, antes

    if atual.empty:
        # Concatenar com um DataFrame vazio sem colunas degrada todos os
        # dtypes para object e quebra a escrita do parquet.
        final = novo.copy()
    else:
        datas_novas = set(pd.to_datetime(novo[cfg.COL_DATA]))
        manter = ~pd.to_datetime(atual[cfg.COL_DATA]).isin(datas_novas)
        final = pd.concat([atual[manter], novo], ignore_index=True)

    final = final.drop_duplicates().sort_values(cfg.COL_DATA, kind="stable")
    final = final.reset_index(drop=True)

    _gravar(final)
    log.info("curated: %d -> %d linhas%s", antes, len(final),
             "" if ENGINE else " (CSV apenas — sem engine parquet)")
    return antes, len(final)


def datas_existentes() -> set[date]:
    df = carregar_curated()
    if df.empty:
        return set()
    return set(pd.to_datetime(df[cfg.COL_DATA]).dt.date.unique())


def registrar_lacunas(janela: list[date], com_dados: set[date]) -> int:
    """Mantém o registro de dias úteis sem publicação na fonte.

    A B3 tem pregões sem dado publicado — verificado no próprio site. Sem esse
    registro, quem consumir a série não distingue "a B3 não publicou" de "o
    coletor falhou" nem de "o valor era zero".

    O registro é reconstruído para a janela consultada a cada execução: um dia
    marcado como lacuna que depois for publicado sai da lista sozinho. Datas
    fora da janela são preservadas como histórico.
    """
    cfg.DIR_CURATED.mkdir(parents=True, exist_ok=True)

    anteriores: dict[date, str] = {}
    if LACUNAS.exists():
        antigo = pd.read_csv(LACUNAS, sep=";", encoding="utf-8-sig")
        for _, r in antigo.iterrows():
            anteriores[pd.Timestamp(r["data"]).date()] = str(r["verificado_em"])

    dentro = set(janela)
    # Fora da janela: preserva. Dentro: recalcula.
    registro = {d: v for d, v in anteriores.items() if d not in dentro}

    hoje = date.today().isoformat()
    for d in janela:
        if d not in com_dados:
            registro[d] = anteriores.get(d, hoje)

    if not registro:
        if LACUNAS.exists():
            LACUNAS.unlink()
        return 0

    df = pd.DataFrame(
        [{"data": d.isoformat(),
          "dia_semana": d.strftime("%A"),
          "verificado_em": v}
         for d, v in sorted(registro.items())]
    )
    df.to_csv(LACUNAS, index=False, sep=";", encoding="utf-8-sig")
    return len(df)
