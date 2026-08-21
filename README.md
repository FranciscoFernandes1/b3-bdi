# BDI B3 — coleta diária

Coleta a tabela *Quadro analítico das posições em aberto* do BDI da B3 e mantém
uma série histórica. A fonte só disponibiliza os últimos 21 dias úteis, então a
série existe apenas a partir do momento em que a coleta é ligada.

## Uso

```bash
pip install -r requirements.txt
python -m playwright install chromium    # só para o comando `descobrir`
python run.py sync
```

Saída em `dados/curated/`: `.csv` (separador `;`, decimal `,`) e `.parquet`
quando há engine disponível.

## Comandos

| Comando | Função |
|---|---|
| `sync` | Coleta a janela e consolida. Idempotente. |
| `sync --desde AAAA-MM-DD` | Coleta a partir de uma data específica. |
| `sondar` | Verifica se a tabela mantém histórico. |
| `reprocessar` | Reconstrói o consolidado a partir de `dados/raw/`. |
| `descobrir --date DD-MM-AAAA` | Inspeciona o endpoint da B3. |

## Funcionamento

Cada execução reconsulta os últimos 21 dias corridos e substitui as partições
por data. Execuções perdidas são recuperadas automaticamente pela seguinte,
desde que dentro da janela da fonte.

`dados/raw/` guarda o JSON recebido por pregão, permitindo reprocessar sem
consultar a B3 novamente. `dados/curated/` guarda a tabela consolidada.

O arquivo `*_lacunas.csv` registra dias úteis sem publicação na fonte. Serve
para distinguir ausência de dado de valor zero.

## Notas técnicas

O endpoint responde a `POST` com corpo `{}`; `GET` retorna 405. Os dois
segmentos de data na URL não formam intervalo — o segundo é um limite, e a
consulta é feita por dia.

Nos metadados, o campo `format` indica o formato de exibição (`DD/MM/YYYY`)
enquanto os valores chegam em ISO 8601. As linhas trazem uma posição a mais
que o número de colunas declaradas, sempre nula.

## Automação

O workflow em `.github/workflows/bdi.yml` roda em dias úteis e commita o
resultado. Requer *Read and write permissions* em Settings → Actions → General.
