"""CLI do pipeline.

    python -m b3_bdi.cli descobrir --date 31-07-2026
    python -m b3_bdi.cli sync [--dias 21] [--force]
    python -m b3_bdi.cli reprocessar
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd

from . import __version__
from . import config as cfg
from . import fetch, parse, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("b3_bdi")


# ---------------------------------------------------------------------------
def cmd_descobrir(args) -> int:
    dia = datetime.strptime(args.date, cfg.DATE_FMT_PAGE).date()
    log.info("abrindo a página de %s e capturando o tráfego XHR...", dia)

    capturas = fetch.via_browser(dia, retornar_urls=True)
    if not capturas:
        log.error("nenhuma resposta JSON capturada. A página carregou?")
        return 1

    destino = cfg.ROOT / "dados" / "descoberta"
    destino.mkdir(parents=True, exist_ok=True)

    capturas = sorted(capturas, key=lambda c: len(json.dumps(c[1])), reverse=True)

    print("\n" + "=" * 78)
    print("ENDPOINTS CAPTURADOS")
    print("=" * 78)

    for i, (url, payload, req) in enumerate(capturas, 1):
        colunas = parse.localizar_colunas(payload)
        valores = parse.localizar_valores(payload)
        registros = parse.localizar_registros(payload)
        da_tabela = cfg.TABLE.lower() in url.lower()

        print(f"\n[{i}]{'  <<< ESTA É A TABELA' if da_tabela else ''}")
        print(f"  método   {req['metodo']}")
        print(f"  url      {url}")
        if req["corpo"]:
            print(f"  corpo    {req['corpo'][:600]}")
        if req["content_type"]:
            print(f"  content  {req['content_type']}")
        print(f"  tamanho  {len(json.dumps(payload)):,} bytes")
        print(f"  colunas  {len(colunas)}  |  linhas(lista) {len(valores)}"
              f"  |  linhas(dict) {len(registros)}")
        if colunas:
            print(f"  cabeç.   {[c.get('friendlyNamePt') or c.get('name') for c in colunas]}")
        if valores:
            print(f"  1ª linha {valores[0]}")

        arq = destino / f"{i:02d}_{'TABELA' if da_tabela else 'outro'}.json"
        arq.write_text(json.dumps({"url": url, "requisicao": req,
                                   "payload": payload},
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")

    print("\n" + "=" * 78)
    alvo = next((c for c in capturas if cfg.TABLE.lower() in c[0].lower()), None)
    if alvo:
        _, _, req = alvo
        print(f"Método a configurar: B3_API_METHOD = {req['metodo']}")
        if req["corpo"]:
            print("A requisição tem CORPO — cole o conteúdo acima em B3_API_BODY.")
        else:
            print("A requisição não tem corpo.")
    else:
        print(f"Nenhuma resposta de {cfg.TABLE} foi capturada.")
    print(f"Payloads completos salvos em: {destino}")
    print("=" * 78)
    return 0


def _quadro(paginas: list) -> tuple[dict, int]:
    """Junta as páginas de UMA data e devolve (por_data, total_linhas)."""
    por_data: dict[date, dict] = {}
    total = 0
    for payload in paginas:
        for dia, parte in parse.dividir_por_data(payload).items():
            total += len(parte["values"])
            if dia in por_data:
                por_data[dia]["values"].extend(parte["values"])
            else:
                por_data[dia] = {"columns": parte["columns"],
                                 "values": list(parte["values"])}
    return por_data, total


def _dias_uteis(de: date, ate: date) -> list[date]:
    dias, d = [], de
    while d <= ate:
        if d.weekday() < 5:      # feriados não são filtrados: a B3 simplesmente
            dias.append(d)       # devolve vazio, o que evita manter calendário
        d += timedelta(days=1)
    return dias


def cmd_sync(args) -> int:
    janela = args.dias or cfg.JANELA_DIAS
    hoje = date.today()
    de = args.desde or (hoje - timedelta(days=janela))
    dias = _dias_uteis(de, hoje)

    log.info("consultando %d dia(s) útil(eis) entre %s e %s", len(dias), de, hoje)

    frames, com_dados, vazios, falhas = [], [], [], []

    for i, dia in enumerate(dias):
        if i:
            time.sleep(cfg.PAUSA)

        try:
            paginas = fetch.coletar(dia, dia)
            por_data, total = _quadro(paginas)

            # Vazio é ambíguo: pode ser pregão sem publicação ou limitação de
            # taxa devolvida como 200. Confirmar antes de aceitar.
            if not total and cfg.RECONFERIR_VAZIO:
                log.info("%s veio vazio; reconferindo em %.0fs",
                         dia, cfg.PAUSA_RECONFERIR)
                time.sleep(cfg.PAUSA_RECONFERIR)
                paginas = fetch.coletar(dia, dia)
                por_data, total = _quadro(paginas)
                if total:
                    log.warning("%s tinha dados — o primeiro vazio era falso "
                                "negativo. Considere aumentar B3_PAUSA.", dia)
        except fetch.SemDados:
            vazios.append(dia)
            continue
        except Exception as e:  # noqa: BLE001
            log.error("%s: %s", dia, e)
            falhas.append(dia)
            continue

        if not total:
            vazios.append(dia)
            continue

        for d, parte in por_data.items():
            if d != dia:
                log.warning("pedi %s e a B3 devolveu %s", dia, d)
            store.salvar_raw(parte, d)
            df = parse.normalizar(parte, d)
            if not df.empty:
                frames.append(df)
        com_dados.append(dia)
        log.info("%s: %d linhas", dia, total)

    if frames:
        store.upsert(pd.concat(frames, ignore_index=True))

    # Só registra lacuna onde houve resposta conclusiva: erro de rede não é
    # ausência de dado, e o dia corrente pode simplesmente não ter saído ainda.
    conclusivos = [d for d in dias if d not in falhas and d != date.today()]
    n_lac = store.registrar_lacunas(conclusivos, set(com_dados))
    if n_lac:
        log.info("%d lacuna(s) registrada(s) em %s", n_lac, store.LACUNAS.name)

    log.info("%d com dados, %d sem publicação, %d com erro",
             len(com_dados), len(vazios), len(falhas))
    if vazios:
        log.info("sem dados após reconferência: %s",
                 ", ".join(d.isoformat() for d in vazios))
        uteis = [d for d in vazios if d != date.today()]
        if uteis:
            log.info("Lacunas da própria fonte (confirmado no site da B3 para "
                     "14/08/2026: \"Não foram encontrados valores para este "
                     "período\"). Ficam documentadas em %s.", store.LACUNAS.name)
    if falhas:
        # Não derruba o job: a próxima execução recobre a mesma janela.
        log.error("erros em: %s", ", ".join(d.isoformat() for d in falhas))
    return 0


def cmd_sondar(args) -> int:
    """Descobre até onde o histórico da API vai.

    A página do site só oferece ~15 dias no seletor, mas a API aceita
    intervalo arbitrário. Se ela servir 2022, a série inteira é recuperável
    de uma vez em vez de acumulada dia a dia.
    """
    hoje = date.today()

    # Antes de sondar às cegas, perguntar à própria tabela.
    try:
        recente = fetch.coletar(hoje - timedelta(days=3), hoje)
        if recente:
            lim = parse.limites_da_tabela(recente[0])
            print(f"\nTabela: {lim['nome']}")
            print(f"  hasHistory = {lim['hasHistory']}")
            print(f"  limitDate  = {lim['limitDate']}")
            if lim["hasHistory"] is False:
                print("\n  A B3 declara que esta tabela NÃO guarda histórico.")
                print(f"  O teto é {lim['limitDate']} (dias úteis para trás).")
                print("  A série precisa ser acumulada daqui pra frente:")
                print("      python run.py sync\n")
                return 0
    except Exception as e:  # noqa: BLE001
        log.warning("não foi possível ler os metadados (%s); sondando às cegas", e)

    def dia_util(d: date) -> date:
        """Recua até uma quarta-feira — evita sábado/domingo e a maior parte
        dos feriados emendados, que dariam falso negativo na sondagem."""
        while d.weekday() != 2:
            d -= timedelta(days=1)
        return d

    alvos = [dia_util(date(a, 3, 15)) for a in range(args.desde_ano, hoje.year + 1)]
    alvos.append(dia_util(hoje - timedelta(days=7)))
    alvos = sorted(set(a for a in alvos if a < hoje))

    print(f"\n{'data testada':<14} {'linhas':>8}   resultado")
    print("-" * 52)
    disponiveis = []
    for alvo in alvos:
        try:
            paginas = fetch.coletar(alvo, alvo)
            _, linhas = _quadro(paginas)
        except Exception as e:  # noqa: BLE001
            print(f"{alvo!s:<14} {'--':>8}   erro: {str(e)[:40]}")
            continue
        print(f"{alvo!s:<14} {linhas:>8}   {'OK' if linhas else 'vazio'}")
        if linhas:
            disponiveis.append(alvo)

    print("-" * 52)
    if disponiveis:
        print(f"Histórico confirmado desde pelo menos {min(disponiveis)}.")
        print(f"Para puxar tudo:  python run.py sync --desde {min(disponiveis)}")
    else:
        print("Nenhuma data histórica retornou dados.")
    return 0


def cmd_reprocessar(args) -> int:
    """Reconstrói o curated a partir dos snapshots em raw/, sem tocar na B3."""
    arquivos = sorted(cfg.DIR_RAW.rglob("*.json.gz"))
    if not arquivos:
        log.error("nenhum snapshot em %s", cfg.DIR_RAW)
        return 1

    frames = []
    for caminho in arquivos:
        dia = datetime.strptime(caminho.name.split(".")[0], cfg.DATE_FMT_FILE).date()
        with gzip.open(caminho, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        df = parse.normalizar(payload, dia)
        if not df.empty:
            frames.append(df)

    if cfg.PARQUET.exists():
        cfg.PARQUET.unlink()
    store.upsert(pd.concat(frames, ignore_index=True))
    log.info("reprocessadas %d datas.", len(frames))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="b3_bdi")
    ap.add_argument("--version", action="version",
                    version=f"b3_bdi {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("descobrir", help="captura o endpoint XHR da página")
    p.add_argument("--date", required=True, help="DD-MM-AAAA")
    p.set_defaults(func=cmd_descobrir)

    p = sub.add_parser("sync", help="coleta a janela disponível e consolida")
    p.add_argument("--dias", type=int, default=None,
                   help="janela em dias corridos a partir de hoje")
    p.add_argument("--desde", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                   default=None, help="data inicial explícita (AAAA-MM-DD)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("sondar", help="testa até onde o histórico da API vai")
    p.add_argument("--desde-ano", type=int, default=2022)
    p.set_defaults(func=cmd_sondar)

    p = sub.add_parser("reprocessar", help="reconstrói o curated a partir de raw/")
    p.set_defaults(func=cmd_reprocessar)

    args = ap.parse_args()
    log.info("b3_bdi %s", __version__)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
