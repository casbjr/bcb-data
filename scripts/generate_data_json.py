"""
Gera docs/data.json a partir do bacen_cartao_pipeline.py, no formato que o
painel HTML (docs/index.html) espera.

Uso: python scripts/generate_data_json.py
Roda a partir da raiz do repo (é o que o workflow do GitHub Actions faz).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from bacen_cartao_pipeline import (
    get_sgs_cartao,
    listar_instituicoes_alvo,
    get_ifdata_cartao,
    get_ranking_reclamacoes_cartao,
    SGS_SERIES_CARTAO,
)

OUT_PATH = Path(__file__).parent.parent / "docs" / "data.json"
ROOT_OUT_PATH = Path(__file__).parent.parent / "data.json"

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
    # Ajuste conforme o nome real da coluna de valor no relatório 11
    # (confira df.columns na primeira rodada real)
    col_valor = next((c for c in df.columns if "valor" in c.lower()), None)
    if col_valor is None:
        print("[aviso] coluna de valor não identificada em IF.data - "
              "ajuste build_ifdata_block() manualmente")
        return []

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
        valores = pd.to_numeric(grupo[col_indice], errors="coerce")
        if valores.isna().all():
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



def main():
    sgs_blocks = build_sgs_block()

    # Ajuste os trimestres conforme forem sendo publicados no IF.data
    quarters = [202503]
    try:
        ifdata_blocks = build_ifdata_block(quarters)
    except Exception as e:
        print(f"[aviso] IF.data falhou ({e}) - data.json sai só com SGS")
        ifdata_blocks = []

    # Últimos 4 trimestres pra ranking de reclamações - ajuste conforme
    # forem publicados novos (Bacen costuma soltar ~1 mês após fechar o tri)
    periodos_reclamacoes = [(2025, 3), (2025, 4), (2026, 1)]
    try:
        reclamacoes_blocks = build_reclamacoes_block(periodos_reclamacoes)
        print(reclamacoes_blocks)
    except Exception as e:
        print(f"[aviso] Ranking de reclamações falhou ({e})")
        reclamacoes_blocks = []

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sgs": sgs_blocks,
        "ifdata": ifdata_blocks,
        "reclamacoes": reclamacoes_blocks,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ROOT_OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"data.json gerado em {OUT_PATH} e {ROOT_OUT_PATH} ({len(sgs_blocks)} séries SGS, "
          f"{len(ifdata_blocks)} IF.data, {len(reclamacoes_blocks)} reclamações)")


if __name__ == "__main__":
    main()
