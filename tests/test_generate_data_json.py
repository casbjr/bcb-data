"""
Testes pra generate_data_json.py - cobrem principalmente build_reclamacoes_block()
(critério oficial do Top 15, dedup por CNPJ, fusão de conglomerado, volume
irmão do índice) e build_ifdata_block() (agrupamento por corte de
vencimento, nomes canônicos).
"""
import pandas as pd
import pytest

import generate_data_json as g


# ---------------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bruto,esperado", [
    ("12345678000199", "12345678000199"),
    ("12345678000199.0", "12345678000199"),
    ("", ""),
    (None, ""),
])
def test_normalizar_cnpj(bruto, esperado):
    assert g._normalizar_cnpj(bruto) == esperado


def test_slug():
    assert g._slug("C6 Bank (conglomerado)") == "c6_bank_conglomerado"
    assert g._slug("Vencido a Partir de 15 Dias") == "vencido_a_partir_de_15_dias"


# ---------------------------------------------------------------------------
# build_ifdata_block
# ---------------------------------------------------------------------------

def test_build_ifdata_block_agrupa_por_banco_e_corte(monkeypatch):
    df = pd.DataFrame([
        {"NomeInstituicao": "PORTO SEGURO", "tier": "concorrente", "NomeColuna": "Total", "Saldo": 4.6e9, "AnoMes": 202603},
        {"NomeInstituicao": "PORTO SEGURO", "tier": "concorrente", "NomeColuna": "Total", "Saldo": 4.3e9, "AnoMes": 202512},
        {"NomeInstituicao": "PORTO SEGURO", "tier": "concorrente", "NomeColuna": "Vencido a Partir de 15 Dias", "Saldo": 0.21e9, "AnoMes": 202603},
    ])
    monkeypatch.setattr(g, "get_ifdata_cartao", lambda quarters: df)

    blocks = g.build_ifdata_block([202512, 202603])
    por_key = {b["key"]: b for b in blocks}

    assert por_key["porto_total"]["dates"] == ["202512", "202603"]
    assert por_key["porto_total"]["values"] == [4.3, 4.6]
    assert por_key["porto_total"]["group"] == "Porto"  # NOME_CANONICO, não a razão social
    assert por_key["porto_vencido_a_partir_de_15_dias"]["values"] == [0.21]


def test_build_ifdata_block_vazio_quando_get_ifdata_cartao_vazio(monkeypatch):
    monkeypatch.setattr(g, "get_ifdata_cartao", lambda quarters: pd.DataFrame())
    assert g.build_ifdata_block([202603]) == []


# ---------------------------------------------------------------------------
# build_reclamacoes_block - critério oficial do Top 15 (Nota Técnica Bacen)
# ---------------------------------------------------------------------------

def _linha(nome, cnpj, indice, clientes, procedentes, ano=2026, periodo=1):
    return {
        "Instituição financeira": nome,
        "CNPJ IF": cnpj,
        "Índice": indice,
        "Quantidade total de clientes – CCS e SCR": clientes,
        "Quantidade de reclamações procedentes extrapoladas": procedentes,
        "Ano": ano,
        "Periodo": periodo,
    }


# As 15 instituições reais do print da página pública do Bacen usado pra
# validar o fix (posição, nome, índice batendo exato).
_TOP15_OFICIAL = [
    ("BANCO C6 (conglomerado)", "1", "55,30", "34.326.775", "2.000"),
    ("BRADESCO (conglomerado)", "2", "48,92", "110.328.071", "5.000"),
    ("BTG PACTUAL/BANCO PAN (conglomerado)", "3", "42,81", "27.389.127", "1.200"),
    ("PICPAY", "4", "37,22", "68.839.517", "2.600"),
    ("INTER", "5", "34,86", "43.178.240", "1.500"),
    ("SANTANDER (conglomerado)", "6", "34,49", "71.632.000", "2.500"),
    ("ITAU", "7", "34,12", "100.863.694", "3.400"),
    ("NEON PAGAMENTOS IP", "8", "30,55", "26.732.860", "800"),
    ("MERCADO PAGO IP", "9", "30,26", "71.283.946", "2.200"),
    ("PAGSEGURO", "10", "27,83", "33.925.503", "950"),
    ("CAIXA ECONOMICA FEDERAL", "11", "23,12", "158.194.617", "3.700"),
    ("BB", "12", "19,36", "82.994.859", "1.600"),
    ("NU PAGAMENTOS", "13", "10,65", "114.737.784", "1.200"),
    ("99PAY IP", "14", "9,51", "27.324.850", "260"),
    ("CLOUDWALK IP", "15", "6,67", "18.158.220", "120"),
]


def test_top15_bate_com_pagina_oficial_do_bacen(monkeypatch):
    linhas = [_linha(*args) for args in _TOP15_OFICIAL]
    df = pd.DataFrame(linhas)
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 1)])
    indice_blocks = sorted((b for b in blocks if b["metrica"] == "índice"), key=lambda b: b["rank"])

    assert [b["rank"] for b in indice_blocks] == list(range(1, 16))
    assert [b["values"][-1] for b in indice_blocks] == [
        55.3, 48.92, 42.81, 37.22, 34.86, 34.49, 34.12, 30.55, 30.26,
        27.83, 23.12, 19.36, 10.65, 9.51, 6.67,
    ]


def test_top15_e_criterio_de_clientes_nao_de_indice(monkeypatch):
    # Bancos médios reais com índice pior que qualquer um dos 15 grandes,
    # mas poucos clientes - NÃO podem entrar no Top 15 (a entrada é por
    # PORTE, a ordem é que usa índice). Esse foi o bug que motivou reler a
    # Nota Técnica: um piso de clientes sobre o índice nunca replicava
    # isso corretamente.
    linhas = [_linha(*args) for args in _TOP15_OFICIAL]
    linhas.append(_linha("FACTA S.A. CFI (conglomerado)", "90", "984,99", "1.100.000", "1.000"))
    linhas.append(_linha("BRB (conglomerado)", "91", "386,13", "2.500.000", "900"))
    df = pd.DataFrame(linhas)
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 1)])
    grupos_no_top15 = {b["group"] for b in blocks if b["metrica"] == "índice" and b["rank"] <= 15}

    assert "FACTA S.A. CFI (conglomerado)" not in grupos_no_top15
    assert "BRB (conglomerado)" not in grupos_no_top15
    assert len(grupos_no_top15) == 15


def test_porto_sempre_aparece_com_posicao_real_fora_do_top15(monkeypatch):
    linhas = [_linha(*args) for args in _TOP15_OFICIAL]
    linhas.append(_linha("PORTO SEGURO (conglomerado)", "99", "150,52", "900.000", "135"))
    df = pd.DataFrame(linhas)
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 1)])
    porto = next(b for b in blocks if b["metrica"] == "índice" and "porto" in b["key"])

    assert porto["destaque"] is True
    assert porto["rank"] == 16  # única fora do Top 15, único candidato do bucket "Demais"


def test_porto_ja_no_top15_por_porte_nao_duplica(monkeypatch):
    linhas = [
        _linha("PORTO SEGURO (conglomerado)", "99", "150,52", "20.000.000", "900"),
        _linha("BANCO C6 (conglomerado)", "1", "55,30", "34.000.000", "2.000"),
    ]
    df = pd.DataFrame(linhas)
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 1)])
    indice_blocks = [b for b in blocks if b["metrica"] == "índice"]

    assert len(indice_blocks) == 2  # não duplica a Porto
    porto = next(b for b in indice_blocks if "porto" in b["key"])
    assert porto["rank"] == 1  # pior índice das duas, ordenada corretamente
    assert porto["destaque"] is True


def test_dedup_por_cnpj_funde_grafias_diferentes_do_mesmo_banco(monkeypatch):
    # Bug real: o Bacen mudou o nome de exibição do C6 entre trimestres -
    # sem agrupar por CNPJ, isso virava duas séries fragmentadas em vez de
    # uma série contínua.
    df = pd.DataFrame([
        _linha("BANCO C6 (conglomerado)", "100", "10,5", "34.000.000", "2.000", periodo=3),
        _linha("C6 BANK (conglomerado)", "100", "12,0", "35.000.000", "2.200", periodo=4),
    ])
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 3), (2026, 4)])
    indice_blocks = [b for b in blocks if b["metrica"] == "índice"]

    assert len(indice_blocks) == 1
    assert indice_blocks[0]["values"] == [10.5, 12.0]


def test_conglomerado_com_dois_bancos_alvo_nao_perde_nenhum(monkeypatch):
    # BTG comprou o Pan e o Bacen passa a publicar os dois como uma
    # entidade única - não pode silenciosamente virar só "concorrente"
    # (tier do Pan) ou só "benchmark" (tier do BTG).
    df = pd.DataFrame([
        _linha("BTG PACTUAL/BANCO PAN (conglomerado)", "200", "20,0", "27.000.000", "1.200"),
    ])
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 1)])
    bloco = next(b for b in blocks if b["metrica"] == "índice")

    assert bloco["tier"] == "conglomerado"
    assert bloco["key"] == "btg+pan"
    assert "BTG Pactual" in bloco["group"] and "Banco Pan" in bloco["group"]


def test_valor_indice_invalido_e_descartado_nao_vira_zero(monkeypatch):
    # Um índice "0" entraria como o melhor resultado possível - enganoso
    # se na verdade é só um erro de parsing/valor ausente.
    df = pd.DataFrame([
        _linha("BANCO C6 (conglomerado)", "1", "55,30", "34.000.000", "2.000"),
        _linha("BRADESCO (conglomerado)", "2", " ", "110.000.000", "5.000"),
    ])
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 1)])
    grupos = {b["group"] for b in blocks if b["metrica"] == "índice"}
    assert "BRADESCO (conglomerado)" not in grupos


def test_volume_e_serie_irmao_do_indice(monkeypatch):
    df = pd.DataFrame([
        _linha("BANCO C6 (conglomerado)", "1", "55,30", "34.000.000", "2.000"),
    ])
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 1)])
    indice = next(b for b in blocks if b["metrica"] == "índice")
    volume = next(b for b in blocks if b["metrica"] == "volume")

    assert volume["rank"] == indice["rank"]
    assert volume["group"] == indice["group"]
    assert volume["unit"] == "reclamações"
    assert volume["values"] == [2000]


def test_sem_coluna_de_clientes_cai_pro_criterio_antigo_sem_devolver_vazio(monkeypatch):
    df = pd.DataFrame([
        {"Instituição financeira": "BANCO C6 (conglomerado)", "CNPJ IF": "1",
         "Índice": "55,30", "Ano": 2026, "Periodo": 1},
    ])
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: df)

    blocks, _ = g.build_reclamacoes_block([(2026, 1)])
    assert len(blocks) >= 1


def test_ranking_de_reclamacoes_vazio_devolve_listas_vazias(monkeypatch):
    monkeypatch.setattr(g, "get_ranking_reclamacoes_cartao", lambda periodos: pd.DataFrame())
    blocks, periodos_ok = g.build_reclamacoes_block([(2026, 1)])
    assert blocks == []
    assert periodos_ok == []


# ---------------------------------------------------------------------------
# Janelas de período (histórico)
# ---------------------------------------------------------------------------

def test_periodos_reclamacoes_recentes_retorna_n_periodos():
    periodos = g.periodos_reclamacoes_recentes(6)
    assert len(periodos) == 6
    assert periodos == sorted(periodos)  # cronológico


def test_quarters_ifdata_recentes_retorna_n_trimestres():
    quarters = g.quarters_ifdata_recentes(6)
    assert len(quarters) == 6
    assert quarters == sorted(quarters)
