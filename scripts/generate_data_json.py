"""
Gera docs/data.json a partir do bacen_cartao_pipeline.py, no formato que o
painel HTML (docs/index.html) espera.

Uso: python scripts/generate_data_json.py
Roda a partir da raiz do repo (é o que o workflow do GitHub Actions faz).
"""

import json
import os
import sys
from datetime import datetime, timezone, date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from bacen_cartao_pipeline import (
    get_sgs_cartao,
    listar_instituicoes_alvo,
    get_ifdata_cartao,
    get_ranking_reclamacoes_cartao,
    get_quarters,
    SGS_SERIES_CARTAO,
)

OUT_PATH = Path(__file__).parent.parent / "docs" / "data.json"
ROOT_OUT_PATH = Path(__file__).parent.parent / "data.json"

# Liga logs extras de diagnóstico (ex.: request cru quando IF.data falha).
# Fica desligado por padrão pra não poluir o log do Actions toda semana -
# ative com DEBUG_BCB=1 quando estiver investigando um problema.
DEBUG = os.environ.get("DEBUG_BCB") == "1"

# Rótulo, unidade e tipo (PF/PJ/Total) de cada série SGS, pra exibição e
# filtro no painel
SGS_META = {
    "inadimplencia_total_sfn": ("Inadimplência total · SFN", "%", "Total"),
    "inadimplencia_cartao_total_pf": ("Inadimplência cartão total · PF", "%", "PF"),
    "inadimplencia_cartao_rotativo_pf": ("Inadimplência rotativo · PF", "%", "PF"),
    "inadimplencia_cartao_rotativo_pj": ("Inadimplência rotativo · PJ", "%", "PJ"),
    "inadimplencia_cartao_parcelado_pj": ("Inadimplência parcelado · PJ", "%", "PJ"),
    "juros_cartao_rotativo_pf": ("Juros médios rotativo · PF", "% a.m.", "PF"),
    "juros_cartao_total_pf": ("Juros médios cartão total · PF", "% a.m.", "PF"),
    "juros_cartao_rotativo_pj": ("Juros médios rotativo · PJ", "% a.m.", "PJ"),
    # ATENÇÃO: não confirmei a unidade exata (R$ mil vs R$ milhões) dessas 4
    # séries de saldo contra o metadado oficial do SGS - rode uma vez e
    # confira a ordem de grandeza do valor bruto antes de confiar no rótulo
    # "R$ mi" abaixo (ajuste o rótulo, ou aplique /1000 aqui se necessário).
    "saldo_cartao_total_pf": ("Saldo carteira cartão total · PF", "R$ mi", "PF"),
    "saldo_cartao_rotativo_pf": ("Saldo carteira rotativo · PF", "R$ mi", "PF"),
    "saldo_cartao_parcelado_pf": ("Saldo carteira parcelado · PF", "R$ mi", "PF"),
    "saldo_cartao_total_pj": ("Saldo carteira cartão total · PJ", "R$ mi", "PJ"),
    "saldo_cartao_rotativo_pj": ("Saldo carteira rotativo · PJ", "R$ mi", "PJ"),
    "saldo_cartao_parcelado_pj": ("Saldo carteira parcelado · PJ", "R$ mi", "PJ"),
    "inadimplencia_cartao_parcelado_pf": ("Inadimplência parcelado · PF", "%", "PF"),
    "juros_cartao_parcelado_pf": ("Juros médios parcelado · PF", "% a.m.", "PF"),
    "juros_cartao_parcelado_pj": ("Juros médios parcelado · PJ", "% a.m.", "PJ"),
    "saldo_carteira_total_sfn": ("Saldo carteira total · SFN (todos os produtos)", "R$ mi", "Total"),
}


def build_sgs_block():
    df = get_sgs_cartao(start="2024-01-01")
    df = df.dropna(how="all")
    blocks = []
    for key in SGS_SERIES_CARTAO:
        if key not in df.columns:
            continue
        serie = df[key].dropna()
        if serie.empty:
            continue
        label, unit, tipo = SGS_META.get(key, (key, "", "Total"))
        blocks.append({
            "key": key,
            "label": label,
            "unit": unit,
            "type": tipo,
            "dates": [d.strftime("%b/%y") for d in serie.index],
            "values": [round(float(v), 2) for v in serie.values],
        })
    return blocks


def build_ifdata_block(quarters):
    df = get_ifdata_cartao(quarters)
    if df.empty:
        return []
    blocks = []

    col_valor = next((c for c in df.columns if "valor" in c.lower()), None)
    if col_valor is None:
        print("[aviso] coluna de valor não identificada em IF.data - "
              "ajuste build_ifdata_block() manualmente")
        return []

    # Tratamento contra NaN para não quebrar a estrutura do JSON
    df[col_valor] = df[col_valor].fillna(0)

    for nome, grupo in df.groupby("NomeInstituicao"):
        grupo = grupo.sort_values("AnoMes")
        tier = grupo["tier"].iloc[0] if "tier" in grupo.columns else "outro"
        blocks.append({
            "key": nome.lower().replace(" ", "_"),
            "label": f"{nome} · carteira cartão PF",
            "unit": "R$ bi",
            "group": nome,
            "tier": tier,
            "dates": [str(a) for a in grupo["AnoMes"]],
            "values": [round(float(v) / 1e9, 2) for v in grupo[col_valor]],
        })
    return blocks


def build_reclamacoes_block(periodos):
    df = get_ranking_reclamacoes_cartao(periodos)
    if df.empty:
        return []

    col_instituicao = next((c for c in df.columns if "institui" in c.lower()), None)
    col_indice = next((c for c in df.columns if "indice" in c.lower() or "índice" in c.lower()), None)
    if col_instituicao is None or col_indice is None:
        print(f"[aviso] colunas não identificadas no ranking - colunas: {list(df.columns)}")
        return []

    blocks = []
    for nome, grupo in df.groupby(col_instituicao):
        grupo = grupo.sort_values(["Ano", "Periodo"])

        # Converte strings BR para float americano e substitui nulos gerados por erro por 0
        valores = pd.to_numeric(
            grupo[col_indice].astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
            errors="coerce"
        ).fillna(0)

        if valores.empty:
            continue

        tier = grupo["tier"].iloc[0] if "tier" in grupo.columns else "outro"
        blocks.append({
            "key": str(nome).lower().replace(" ", "_"),
            "label": f"{nome} · índice de reclamações",
            "unit": "pontos",
            "group": nome,
            "tier": tier,
            "dates": [f"{p}Q{str(a)[2:]}" for a, p in zip(grupo["Ano"], grupo["Periodo"])],
            "values": [round(float(v), 2) for v in valores],
        })
    return blocks


def periodos_reclamacoes_recentes(n: int = 3) -> list[tuple[int, int]]:
    """Gera os últimos `n` (ano, trimestre) civis, contando o trimestre
    corrente pra trás. Não considera defasagem de publicação aqui porque
    o ranking de reclamações costuma sair mais rápido que o IF.data
    (~30-45d); se um período ainda não estiver publicado, o próprio
    baixar_ranking_reclamacoes vai logar aviso e ele fica de fora do
    resultado sem quebrar o pipeline."""
    hoje = date.today()
    trimestre_atual = (hoje.month - 1) // 3 + 1
    periodos = []
    ano, tri = hoje.year, trimestre_atual
    for _ in range(n):
        periodos.append((ano, tri))
        tri -= 1
        if tri == 0:
            tri = 4
            ano -= 1
    return list(reversed(periodos))


def main():
    # Proteção do bloco SGS para instabilidades da API do Bacen
    try:
        sgs_blocks = build_sgs_block()
        print(f"[sucesso] SGS processado com {len(sgs_blocks)} séries.")
    except Exception as e:
        print(f"[aviso] SGS falhou ({e}) - data.json sai sem dados do SGS")
        sgs_blocks = []

    # Trimestres do IF.data calculados automaticamente (considera a
    # defasagem de publicação de ~75 dias) em vez de hardcoded - não
    # precisa mais editar esse número toda vez que sai um trimestre novo.
    quarters = get_quarters(date.today().year)
    if not quarters:
        # início de ano: ainda não saiu nenhum trimestre do ano corrente,
        # usa o(s) último(s) do ano anterior
        quarters = get_quarters(date.today().year - 1)[-1:]
    try:
        ifdata_blocks = build_ifdata_block(quarters)
        print(f"[sucesso] IF.data processado com {len(ifdata_blocks)} blocos (trimestres: {quarters}).")
    except Exception as e:
        print(f"[aviso] IF.data falhou ({e}) - data.json segue sem dados de carteira")

        if DEBUG:
            import requests
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            for anomes in quarters:
                url = (f"https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata/"
                       f"IfDataCadastro?$filter=AnoMes eq {anomes}&$top=3&$format=json")
                try:
                    r = requests.get(url, headers=headers, timeout=30)
                    print(f"[debug-raw] GET {url}")
                    print(f"[debug-raw] status={r.status_code} headers_content_type="
                          f"{r.headers.get('content-type')}")
                    print(f"[debug-raw] body[:500]={r.text[:500]!r}")
                except Exception as e2:
                    print(f"[debug-raw] falha até no request cru: {e2}")
        ifdata_blocks = []

    # Últimos trimestres para o ranking de reclamações, calculados
    # automaticamente em vez de hardcoded.
    periodos_reclamacoes = periodos_reclamacoes_recentes(3)
    try:
        reclamacoes_blocks = build_reclamacoes_block(periodos_reclamacoes)
        print(f"[sucesso] Reclamações processadas com {len(reclamacoes_blocks)} blocos "
              f"(períodos: {periodos_reclamacoes}).")
    except Exception as e:
        print(f"[aviso] Ranking de reclamações falhou ({e})")
        reclamacoes_blocks = []

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sgs": sgs_blocks,
        "ifdata": ifdata_blocks,
        "reclamacoes": reclamacoes_blocks,
    }

    # Escrita segura dos arquivos de dados
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ROOT_OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"data.json gerado com sucesso em {OUT_PATH} e {ROOT_OUT_PATH} ("
          f"{len(sgs_blocks)} séries SGS, {len(ifdata_blocks)} IF.data, {len(reclamacoes_blocks)} reclamações)")


if __name__ == "__main__":
    main()
