"""
Testes pra bacen_cartao_pipeline.py, cobrindo os bugs sutis que já
morderam esse pipeline uma vez: falso-positivo de termo curto, entidade
errada escolhida quando várias compartilham o mesmo código de
conglomerado, e escopo de modalidade/corte do IF.data.
"""
import pandas as pd
import pytest

import bacen_cartao_pipeline as p


# ---------------------------------------------------------------------------
# Matching de nome (_padrao_termos, identificar_tier, identificar_bancos_alvo)
# ---------------------------------------------------------------------------

def test_termo_curto_nao_bate_em_nome_nao_relacionado():
    # "INTER" sozinho bateria como substring em "INTERMEDIUM" sem fronteira
    # de palavra - exatamente o bug que motivou _padrao_termos().
    padrao = p._padrao_termos(["INTER"])
    assert not pd.Series(["BANCO INTERMEDIUM S.A."]).str.contains(padrao, case=False).iloc[0]
    assert pd.Series(["BANCO INTER S.A."]).str.contains(padrao, case=False).iloc[0]


def test_identificar_bancos_alvo_conglomerado_btg_pan():
    # Uma linha só que junta dois bancos-alvo (BTG comprou o Pan) precisa
    # retornar AMBOS, não só o primeiro que bater.
    chaves = p.identificar_bancos_alvo("BTG PACTUAL/BANCO PAN (conglomerado)")
    assert set(chaves) == {"btg", "pan"}


def test_identificar_bancos_alvo_incluir_curtos_false_ignora_marca_curta():
    # "ITAU" sozinho só é seguro pro Ranking de Reclamações (lista curada
    # pequena) - o cadastro do IF.data (milhares de instituições) precisa
    # do nome completo pra não bater em nomes que só mencionam o banco.
    assert p.identificar_bancos_alvo("ITAU", incluir_curtos=True) == ["itau"]
    assert p.identificar_bancos_alvo("ITAU", incluir_curtos=False) == []
    assert p.identificar_bancos_alvo("ITAÚ UNIBANCO S.A.", incluir_curtos=False) == ["itau"]


def test_cooperativa_que_so_menciona_banco_nao_bate_sem_termos_curtos():
    # Bug real: "COOPERATIVA ... DOS FUNCIONÁRIOS DAS EMPRESAS ITAÚ" batia
    # no termo curto "ITAÚ" (fronteira de palavra passa, já que "ITAÚ" é
    # uma palavra isolada ali) mesmo não sendo o banco. Com
    # incluir_curtos=False (o que o IF.data usa), não deve bater.
    nome = "COOPERATIVA DE ECONOMIA E CRÉDITO MÚTUO DOS FUNCIONÁRIOS DAS EMPRESAS ITAÚ"
    assert p.identificar_bancos_alvo(nome, incluir_curtos=False) == []
    assert p.identificar_tier(nome, incluir_curtos=False) == "outro"


# ---------------------------------------------------------------------------
# normalizar_codigo / _parece_subsidiaria
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bruto,esperado", [
    ("12345678", "12345678"),
    ("1234.0", "00001234"),
    ("C0080075", "00080075"),
    ("", ""),
    (None, ""),
])
def test_normalizar_codigo(bruto, esperado):
    assert p.normalizar_codigo(bruto) == esperado


@pytest.mark.parametrize("nome,esperado", [
    ("BRADESCO ADMINISTRADORA DE CONSÓRCIOS LTDA.", True),
    ("ITAÚ UNIBANCO VEÍCULOS ADMINISTRADORA DE CONSÓRCIOS LTDA.", True),
    ("INTER PAG INSTITUIÇÃO DE PAGAMENTO S.A.", True),
    ("BANCO BRADESCO S.A.", False),
    ("ITAÚ UNIBANCO S.A.", False),
])
def test_parece_subsidiaria(nome, esperado):
    assert p._parece_subsidiaria(nome) is esperado


# ---------------------------------------------------------------------------
# get_ifdata_cartao - end-to-end com _ifdata_get mockado
# ---------------------------------------------------------------------------

def test_get_ifdata_cartao_prefere_banco_principal_sobre_subsidiaria(monkeypatch):
    # Bug real (visto em produção): banco principal e sua administradora de
    # consórcio compartilham o mesmo CodConglomeradoPrudencial; a
    # subsidiária vinha depois na ordem do cadastro e "ganhava" o
    # mapeamento código->nome, fazendo o card mostrar o nome errado mesmo
    # com o saldo certo.
    def fake_ifdata_get(endpoint, params=None, top=None):
        if endpoint == "IfDataCadastro":
            return [
                {"NomeInstituicao": "BANCO BRADESCO S.A.", "CodInst": "1",
                 "CodConglomeradoPrudencial": "9999"},
                {"NomeInstituicao": "BRADESCO ADMINISTRADORA DE CONSÓRCIOS LTDA.", "CodInst": "2",
                 "CodConglomeradoPrudencial": "9999"},
            ]
        if endpoint == "IfDataValores":
            return [{"CodInst": "9999", "Grupo": "Cartão de Crédito", "NomeColuna": "Total", "Saldo": 1000.0}]
        return []

    monkeypatch.setattr(p, "_ifdata_get", fake_ifdata_get)
    resultado = p.get_ifdata_cartao([202603])

    assert not resultado.empty
    assert resultado["NomeInstituicao"].tolist() == ["BANCO BRADESCO S.A."]


def test_get_ifdata_cartao_mantem_so_cartao_de_credito_com_todos_os_cortes(monkeypatch):
    def fake_ifdata_get(endpoint, params=None, top=None):
        if endpoint == "IfDataCadastro":
            return [{"NomeInstituicao": "PORTO SEGURO", "CodInst": "1"}]
        if endpoint == "IfDataValores":
            return [
                {"CodInst": "1", "Grupo": "Cartão de Crédito", "NomeColuna": "Total", "Saldo": 4.6e9},
                {"CodInst": "1", "Grupo": "Cartão de Crédito", "NomeColuna": "Vencido a Partir de 15 Dias", "Saldo": 0.12e9},
                {"CodInst": "1", "Grupo": "Empréstimo com Consignação em Folha", "NomeColuna": "Total", "Saldo": 2.0e9},
            ]
        return []

    monkeypatch.setattr(p, "_ifdata_get", fake_ifdata_get)
    resultado = p.get_ifdata_cartao([202603])

    assert set(resultado["Grupo"]) == {"Cartão de Crédito"}
    assert set(resultado["NomeColuna"]) == {"Total", "Vencido a Partir de 15 Dias"}


def test_get_ifdata_cartao_descarta_linha_duplicada_exata(monkeypatch):
    # Bug real visto em produção: um trimestre (ex.: retificação do Bacen)
    # veio com a MESMA linha (instituição/corte/valor) repetida 3x, fazendo
    # o histórico mostrar o mesmo trimestre 3 vezes seguidas com valor
    # idêntico. Duplicata exata deve ser descartada.
    def fake_ifdata_get(endpoint, params=None, top=None):
        if endpoint == "IfDataCadastro":
            return [{"NomeInstituicao": "PORTO SEGURO", "CodInst": "1"}]
        if endpoint == "IfDataValores":
            linha = {"CodInst": "1", "Grupo": "Cartão de Crédito", "Conta": "1", "NomeColuna": "Total", "Saldo": 4.6e9}
            return [linha, dict(linha), dict(linha)]
        return []

    monkeypatch.setattr(p, "_ifdata_get", fake_ifdata_get)
    resultado = p.get_ifdata_cartao([202603])

    assert len(resultado) == 1
    assert resultado["Saldo"].iloc[0] == 4.6e9


def test_get_ifdata_cartao_preserva_linhas_com_valores_diferentes(monkeypatch):
    # Duas linhas do MESMO corte/trimestre com valores DIFERENTES não são
    # duplicata (podem ser duas entidades reais distintas) - o dedup só
    # remove igualdade exata, nunca decide sozinho qual valor "vale".
    def fake_ifdata_get(endpoint, params=None, top=None):
        if endpoint == "IfDataCadastro":
            return [{"NomeInstituicao": "PORTO SEGURO", "CodInst": "1"}]
        if endpoint == "IfDataValores":
            return [
                {"CodInst": "1", "Grupo": "Cartão de Crédito", "Conta": "1", "NomeColuna": "Total", "Saldo": 4.6e9},
                {"CodInst": "1", "Grupo": "Cartão de Crédito", "Conta": "1", "NomeColuna": "Total", "Saldo": 2.1e9},
            ]
        return []

    monkeypatch.setattr(p, "_ifdata_get", fake_ifdata_get)
    resultado = p.get_ifdata_cartao([202603])

    assert len(resultado) == 2


def test_get_ifdata_cartao_vazio_quando_relatorio_nao_tem_cartao(monkeypatch):
    def fake_ifdata_get(endpoint, params=None, top=None):
        if endpoint == "IfDataCadastro":
            return [{"NomeInstituicao": "PORTO SEGURO", "CodInst": "1"}]
        if endpoint == "IfDataValores":
            return [{"CodInst": "1", "NomeColuna": "Total", "Saldo": 100.0}]  # sem campo Grupo
        return []

    monkeypatch.setattr(p, "_ifdata_get", fake_ifdata_get)
    resultado = p.get_ifdata_cartao([202603])
    assert resultado.empty


# ---------------------------------------------------------------------------
# get_quarters
# ---------------------------------------------------------------------------

def test_get_quarters_so_inclui_trimestres_ja_defasados():
    # Um ano bem antigo: todo trimestre já passou da defasagem, os 4
    # devem aparecer.
    assert p.get_quarters(2020) == [202003, 202006, 202009, 202012]
