"""
Gera docs/data.json a partir do bacen_cartao_pipeline.py, no formato que o
painel HTML (docs/index.html) espera.

Uso: python scripts/generate_data_json.py
Roda a partir da raiz do repo (é o que o workflow do GitHub Actions faz).
"""

import json
import os
import re
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
    BANCOS_ALVO,
    identificar_bancos_alvo,
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
    "juros_cartao_rotativo_pf": ("Juros médios rotativo · PF", "% a.a.", "PF"),
    "juros_cartao_total_pf": ("Juros médios cartão total · PF", "% a.a.", "PF"),
    "juros_cartao_rotativo_pj": ("Juros médios rotativo · PJ", "% a.a.", "PJ"),
    "saldo_cartao_total_pf": ("Saldo carteira cartão total · PF", "R$ mi", "PF"),
    "saldo_cartao_rotativo_pf": ("Saldo carteira rotativo · PF", "R$ mi", "PF"),
    "saldo_cartao_parcelado_pf": ("Saldo carteira parcelado · PF", "R$ mi", "PF"),
    "saldo_cartao_total_pj": ("Saldo carteira cartão total · PJ", "R$ mi", "PJ"),
    "saldo_cartao_rotativo_pj": ("Saldo carteira rotativo · PJ", "R$ mi", "PJ"),
    "saldo_cartao_parcelado_pj": ("Saldo carteira parcelado · PJ", "R$ mi", "PJ"),
    "inadimplencia_cartao_parcelado_pf": ("Inadimplência parcelado · PF", "%", "PF"),
    "juros_cartao_parcelado_pf": ("Juros médios parcelado · PF", "% a.a.", "PF"),
    "juros_cartao_parcelado_pj": ("Juros médios parcelado · PJ", "% a.a.", "PJ"),
    "saldo_carteira_total_sfn": ("Saldo carteira total · SFN (todos os produtos)", "R$ mi", "Total"),
}


# Abreviação de mês em pt-BR, escrita à mão (NÃO usar strftime("%b") aqui:
# é dependente do locale da máquina que roda o script - no runner do
# GitHub Actions o locale padrão é "C", que gera "May", "Jun", "Jul" em
# inglês em vez de "mai", "jun", "jul". O parseRoughDate() do index.html
# só reconhece as abreviações em português (meses = {jan:0, fev:1, ...}),
# então qualquer coisa em inglês cai no fallback e é lida como se fosse
# janeiro - o card mostra a data certa mas calcula o "Nd atrás" errado.
MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun",
            "jul", "ago", "set", "out", "nov", "dez"]


def fmt_mes_ano(d) -> str:
    return f"{MESES_PT[d.month - 1]}/{d.strftime('%y')}"


# Nome de exibição estável por banco-alvo, independente de como o Bacen
# grafou a instituição no arquivo daquele trimestre (evita cards duplicados
# quando o nome oficial muda de um período pro outro).
NOME_CANONICO = {
    "porto": "Porto",
    "pan": "Banco Pan",
    "bv": "BV",
    "inter": "Inter",
    "c6": "C6 Bank",
    "itau": "Itaú",
    "bradesco": "Bradesco",
    "santander": "Santander",
    "btg": "BTG Pactual",
    "nubank": "Nubank",
}


# Prefixo do key -> métrica, usado como segunda camada de filtro no painel
# (mesmo padrão de "tier" já usado nas seções de IF.data/Reclamações).
def _metrica_da_key(key: str) -> str:
    if key.startswith("saldo_"):
        return "saldo"
    if key.startswith("inadimplencia_"):
        return "inadimplencia"
    if key.startswith("juros_"):
        return "juros"
    return "outro"


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
            "group": tipo,  # espelha 'type' - reaproveita o mesmo filtro de 2 camadas do IF.data/Reclamações
            "tier": _metrica_da_key(key),
            "dates": [fmt_mes_ano(d) for d in serie.index],
            "values": [round(float(v), 2) for v in serie.values],
        })
    return blocks


def build_ifdata_block(quarters):
    df = get_ifdata_cartao(quarters)
    if df.empty:
        print("[aviso] get_ifdata_cartao() voltou vazio - ver aviso acima com o motivo "
              "(instituição não encontrada, relatório vazio, ou coluna de cartão não identificada)")
        return []
    blocks = []

    # O campo com o valor em R$ se chama "Saldo" no retorno bruto do
    # Bacen (confirmado inspecionando o dump de registros brutos) - não
    # contém "valor" no nome, por isso o "saldo" também entra na busca.
    col_valor = next((c for c in df.columns if "valor" in c.lower() or "saldo" in c.lower()), None)
    if col_valor is None:
        print("[aviso] coluna de valor não identificada em IF.data - "
              "ajuste build_ifdata_block() manualmente")
        return []

    # Tratamento contra NaN para não quebrar a estrutura do JSON
    df[col_valor] = df[col_valor].fillna(0)

    # Um bloco por (banco, corte de vencimento) - só Cartão de Crédito
    # (já filtrado em get_ifdata_cartao), mas com todos os cortes: Total,
    # Vencido a Partir de 15 Dias, A Vencer em até 90 Dias etc. - mesmo
    # detalhamento que o export manual do site (dados.csv) mostra por
    # modalidade. O painel filtra por corte (default "Total").
    for (nome, corte), grupo in df.groupby(["NomeInstituicao", "NomeColuna"]):
        grupo = grupo.sort_values("AnoMes")
        tier = grupo["tier"].iloc[0] if "tier" in grupo.columns else "outro"

        # Nome de exibição curto (NOME_CANONICO) em vez da razão social
        # completa do cadastro do Bacen, que pode ser bem longa.
        chaves = identificar_bancos_alvo(nome, incluir_curtos=False)
        chave = chaves[0] if chaves else None
        nome_exibicao = NOME_CANONICO.get(chave, nome) if chave else nome

        blocks.append({
            "key": f"{chave or _slug(nome)}_{_slug(corte)}",
            "label": f"{nome_exibicao} · Cartão de Crédito · {corte} PF",
            "unit": "R$ bi",
            "group": nome_exibicao,
            "tier": tier,
            "modalidade": corte,
            "dates": [str(a) for a in grupo["AnoMes"]],
            "values": [round(float(v) / 1e9, 2) for v in grupo[col_valor]],
        })
    return blocks


def _slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(texto).strip().lower()).strip("_")


def _normalizar_cnpj(val) -> str:
    """Como normalizar_codigo() do bacen_cartao_pipeline, mas pra CNPJ (14
    dígitos): remove o sufixo '.0' que o pandas cria quando lê a coluna
    como número, e preserva zeros à esquerda."""
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    digitos = "".join(filter(str.isdigit, s))
    return digitos.zfill(14) if digitos else ""


TOP_N_RECLAMACOES = 15

# Critério oficial do Bacen (Nota Técnica do Ranking de Reclamações, seção
# 4): o Ranking contra bancos/financeiras/instituições de pagamento é
# dividido em duas listagens:
#   - "Top 15": as QUINZE INSTITUIÇÕES COM MAIS CLIENTES (não as piores
#     por índice!), ordenadas de forma decrescente por índice.
#   - "Demais": as outras instituições com 30 ou mais reclamações
#     REGULADAS PROCEDENTES no trimestre, também ordenadas por índice
#     decrescente. Quem tem menos de 30 procedentes fica de fora de
#     qualquer ranking (só listada em ordem alfabética à parte).
# Isso explica por que a primeira tentativa (piso de clientes sobre o
# índice) nunca batia com a página pública: o critério de entrada no Top
# 15 não tem nada a ver com o índice em si, é puramente porte (número de
# clientes) - só a ORDEM dentro de cada listagem usa o índice.
LIMIAR_PROCEDENTES_DEMAIS = 30


def build_reclamacoes_block(periodos, top_n: int = TOP_N_RECLAMACOES):
    """Monta o Ranking de Reclamações seguindo a metodologia oficial do
    Bacen: Top N = instituições com mais clientes (ordenadas por índice);
    Porto sempre incluída - com a posição real dela na listagem "Demais"
    quando não estiver entre as N maiores."""
    df = get_ranking_reclamacoes_cartao(periodos)
    if df.empty:
        return [], []

    col_instituicao = next((c for c in df.columns if "institui" in c.lower()), None)
    col_indice = next((c for c in df.columns if "indice" in c.lower() or "índice" in c.lower()), None)
    col_cnpj = next((c for c in df.columns if "cnpj" in c.lower()), None)
    col_clientes = next(
        (c for c in df.columns if "cliente" in c.lower() and "total" in c.lower()),
        None
    )
    col_procedentes = next(
        (c for c in df.columns if "procedentes extrapoladas" in c.lower()),
        None
    )
    if col_instituicao is None or col_indice is None:
        print(f"[aviso] colunas não identificadas no ranking - colunas: {list(df.columns)}")
        return [], []

    # Períodos que de fato vieram com dado (podem ser menos que os
    # solicitados, se algum trimestre ainda não tiver sido publicado).
    periodos_ok = sorted(df[["Ano", "Periodo"]].drop_duplicates().itertuples(index=False, name=None))
    ultimo_periodo = max(periodos_ok) if periodos_ok else None

    df = df.copy()

    def _numero_br(serie):
        return pd.to_numeric(
            serie.astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
            errors="coerce"
        )

    if col_clientes:
        df["valor_clientes"] = _numero_br(df[col_clientes])
    else:
        print("[aviso] coluna de quantidade de clientes não encontrada - não dá pra aplicar o "
              "critério oficial do Top 15 (maior número de clientes).")

    if col_procedentes:
        df["valor_procedentes"] = _numero_br(df[col_procedentes])
    else:
        print("[aviso] coluna de reclamações procedentes extrapoladas não encontrada - não dá pra "
              "aplicar o piso oficial de 30 procedentes da listagem \"Demais\".")

    # Converte strings BR para float americano; valores que não derem pra
    # converter são DESCARTADOS (não viram 0 - um índice de reclamações "0"
    # entraria no ranking como o melhor resultado possível, o que seria
    # enganoso se na verdade é só um erro de parsing).
    df["valor_num"] = _numero_br(df[col_indice])
    invalidos = df[df["valor_num"].isna()]
    if not invalidos.empty:
        print(f"[aviso] {len(invalidos)} valor(es) de índice não-numérico descartado(s): "
              f"{list(zip(invalidos[col_instituicao], invalidos[col_indice]))}")
    df = df.dropna(subset=["valor_num"])

    # Chave de agrupamento: CNPJ da instituição, quando disponível - é o
    # identificador estável de verdade. O nome que o Bacen publica pra
    # mesma instituição pode mudar de um trimestre pro outro (ex.: "BANCO
    # C6" virou "C6 BANK") e agrupar pelo texto puro fragmentava a série em
    # cards duplicados em vez de um histórico contínuo. Cai pro nome
    # (maiúsculo) só se o CNPJ vier vazio pra alguma linha.
    if col_cnpj:
        cnpj_limpo = df[col_cnpj].apply(_normalizar_cnpj)
        nome_upper = df[col_instituicao].astype(str).str.upper().str.strip()
        df["chave_grupo"] = cnpj_limpo.where(cnpj_limpo != "", nome_upper)
    else:
        df["chave_grupo"] = df[col_instituicao].astype(str).str.upper().str.strip()

    grupos = {}
    for chave_grupo, grupo in df.groupby("chave_grupo"):
        grupo = grupo.sort_values(["Ano", "Periodo"])
        nome_recente = str(grupo[col_instituicao].iloc[-1]).strip()
        bancos_alvo_aqui = identificar_bancos_alvo(nome_recente)

        if len(bancos_alvo_aqui) > 1:
            # Conglomerado que junta mais de um banco-alvo numa linha só
            # (ex.: BTG comprou o Pan e o Bacen passou a publicar os dois
            # como uma entidade). Mantemos como um card único em vez de
            # silenciosamente atribuir a linha inteira ao tier de só um
            # dos dois bancos.
            label = " / ".join(NOME_CANONICO.get(c, c) for c in bancos_alvo_aqui)
            tier = "conglomerado"
        elif bancos_alvo_aqui:
            label = NOME_CANONICO.get(bancos_alvo_aqui[0], bancos_alvo_aqui[0])
            tier = BANCOS_ALVO[bancos_alvo_aqui[0]]["tier"]
        else:
            label = nome_recente
            tier = "mercado"

        linha_atual = grupo[(grupo["Ano"] == ultimo_periodo[0]) & (grupo["Periodo"] == ultimo_periodo[1])] \
            if ultimo_periodo else grupo.iloc[0:0]

        def _valor_atual(coluna):
            if linha_atual.empty or coluna not in linha_atual.columns:
                return None
            v = linha_atual[coluna].iloc[0]
            return float(v) if pd.notna(v) else None

        dates_indice = [f"{p}Q{str(a)[2:]}" for a, p in zip(grupo["Ano"], grupo["Periodo"])]

        # Série histórica do volume (reclamações procedentes extrapoladas),
        # pareada com o índice mas descartando pontos sem esse dado (pode
        # faltar em algum trimestre isolado mesmo com o índice presente).
        dates_volume, values_volume = [], []
        if "valor_procedentes" in grupo.columns:
            for data_str, v in zip(dates_indice, grupo["valor_procedentes"]):
                if pd.notna(v):
                    dates_volume.append(data_str)
                    values_volume.append(round(float(v)))

        grupos[chave_grupo] = {
            "key": "+".join(bancos_alvo_aqui) if bancos_alvo_aqui else _slug(label),
            "label": f"{label} · índice de reclamações",
            "unit": "pontos",
            "group": label,
            "tier": tier,
            "metrica": "índice",
            "dates": dates_indice,
            "values": [round(float(v), 2) for v in grupo["valor_num"]],
            "_bancos_alvo": bancos_alvo_aqui,
            "_valor_periodo_atual": _valor_atual("valor_num"),
            "_clientes_periodo_atual": _valor_atual("valor_clientes"),
            "_procedentes_periodo_atual": _valor_atual("valor_procedentes"),
            "_dates_volume": dates_volume,
            "_values_volume": values_volume,
        }

    # Só entram instituições com dado no período mais recente (mesma base
    # de comparação que a página do Bacen usa pra calcular a "Posição" do
    # trimestre atual).
    candidatos = [b for b in grupos.values() if b["_valor_periodo_atual"] is not None]

    # Bucket 1 (oficial, seção 4 da Nota Técnica): "Top N" = as N
    # instituições com MAIS CLIENTES (não as piores por índice!),
    # ordenadas de forma decrescente por índice dentro desse grupo. Se não
    # tivermos dado de clientes pra ninguém (coluna ausente), cai pro
    # critério antigo (pior índice) em vez de devolver uma lista vazia.
    com_clientes = sorted(
        (b for b in candidatos if b["_clientes_periodo_atual"] is not None),
        key=lambda b: b["_clientes_periodo_atual"], reverse=True
    )
    if com_clientes:
        top = com_clientes[:top_n]
    else:
        top = sorted(candidatos, key=lambda b: b["_valor_periodo_atual"], reverse=True)[:top_n]
    top.sort(key=lambda b: b["_valor_periodo_atual"], reverse=True)
    for i, b in enumerate(top, start=1):
        b["rank"] = i

    # Bucket 2 (oficial): "Demais" = instituições fora do Top N com 30+
    # reclamações reguladas procedentes no trimestre, também ordenadas por
    # índice decrescente. Só usamos isso pra achar a posição real da Porto
    # quando ela não estiver entre as N maiores - não exibimos a listagem
    # "Demais" inteira (seria enorme).
    demais = sorted(
        (b for b in candidatos if b not in top and (b["_procedentes_periodo_atual"] or 0) >= LIMIAR_PROCEDENTES_DEMAIS),
        key=lambda b: b["_valor_periodo_atual"], reverse=True
    )
    for i, b in enumerate(demais, start=1):
        b["rank"] = top_n + i

    # A Porto sempre aparece, mesmo fora do Top N, com a posição real dela.
    porto = next((b for b in candidatos if "porto" in b["_bancos_alvo"]), None)
    if porto is not None:
        porto["destaque"] = True
        if "rank" not in porto:
            # Não entrou nem no Top N nem nos "Demais" (provavelmente
            # menos de 30 procedentes no trimestre) - mesmo assim
            # mostramos, com a posição calculada contra o universo inteiro
            # por índice, só pra não sumir do painel.
            universo = sorted(candidatos, key=lambda b: b["_valor_periodo_atual"], reverse=True)
            porto["rank"] = universo.index(porto) + 1
        if porto not in top:
            top = top + [porto]

    bancos_sem_dado = sorted(set(BANCOS_ALVO) - {c for b in grupos.values() for c in b["_bancos_alvo"]})
    if bancos_sem_dado:
        print(f"[aviso] banco(s)-alvo sem nenhuma entrada no ranking de reclamações nos períodos "
              f"{periodos}: {bancos_sem_dado} - confira se o nome oficial no arquivo do Bacen bate "
              f"com os termos em BANCOS_ALVO (rode listar_instituicoes_alvo() ou inspecione "
              f"df[col_instituicao].unique() pra achar o nome certo).")

    print(f"[sucesso] Top {top_n} (oficial: mais clientes, ordenado por índice):")
    for b in top:
        clientes_fmt = f"{b['_clientes_periodo_atual']:,.0f}" if b["_clientes_periodo_atual"] is not None else "?"
        print(f"  #{b.get('rank')} {b['group']} - índice={b['values'][-1] if b['values'] else '?'} - clientes={clientes_fmt}")

    # Bloco irmão de VOLUME (reclamações procedentes extrapoladas) pra cada
    # instituição do Top N - mesma seleção/posição do índice, mas mostrando
    # a quantidade de reclamações em vez do índice normalizado por
    # cliente. É uma série separada (metrica="volume"), filtrável à parte
    # no painel - não substitui o índice.
    volume_blocks = []
    for b in top:
        if b["_dates_volume"]:
            bloco_volume = {
                "key": f"{b['key']}_volume",
                "label": f"{b['group']} · reclamações procedentes",
                "unit": "reclamações",
                "group": b["group"],
                "tier": b["tier"],
                "metrica": "volume",
                "dates": b["_dates_volume"],
                "values": b["_values_volume"],
                "rank": b.get("rank"),
            }
            if b.get("destaque"):
                bloco_volume["destaque"] = True
            volume_blocks.append(bloco_volume)

    for b in top:
        del b["_bancos_alvo"]
        del b["_valor_periodo_atual"]
        del b["_clientes_periodo_atual"]
        del b["_procedentes_periodo_atual"]
        del b["_dates_volume"]
        del b["_values_volume"]

    return top + volume_blocks, periodos_ok


def periodos_reclamacoes_recentes(n: int = 6, defasagem_dias: int = 45) -> list[tuple[int, int]]:
    """Gera os últimos `n` (ano, trimestre) que já devem estar publicados,
    considerando uma defasagem de publicação (chute inicial de 45 dias -
    ajuste se descobrir o prazo real do Bacen pra esse ranking).

    Importante: NUNCA inclui o trimestre em andamento, e só inclui um
    trimestre recém-fechado depois que a defasagem tiver passado. Pedir
    um período que ainda não existe faz o Bacen devolver algo que não é
    CSV, e isso quebra o parser com erro de delimitador.

    Olha 3 anos pra trás (não só o atual e o anterior) pra garantir
    candidatos suficientes mesmo quando n é grande (6) e o ano ainda
    está no começo."""
    hoje = date.today()
    candidatos = []
    for ano in (hoje.year - 2, hoje.year - 1, hoje.year):
        for tri in (1, 2, 3, 4):
            fechamento = date(ano, tri * 3, 28)
            dias_desde_fechamento = (hoje - fechamento).days
            if dias_desde_fechamento >= defasagem_dias:
                candidatos.append((ano, tri))
    return candidatos[-n:]


def quarters_ifdata_recentes(n: int = 6, defasagem_dias: int = 75) -> list[int]:
    """Como periodos_reclamacoes_recentes(), mas pro IF.data (AnoMes no
    formato AAAAMM) - reaproveita get_quarters() por ano, olhando 3 anos
    pra trás pra garantir candidatos suficientes mesmo quando n é grande e
    o ano ainda está no começo. Dá histórico de verdade (múltiplos
    trimestres) em vez de só o trimestre mais recente."""
    hoje = date.today()
    candidatos = []
    for ano in (hoje.year - 2, hoje.year - 1, hoje.year):
        candidatos.extend(get_quarters(ano, defasagem_dias))
    return candidatos[-n:]


def main():
    # Proteção do bloco SGS para instabilidades da API do Bacen
    try:
        sgs_blocks = build_sgs_block()
        print(f"[sucesso] SGS processado com {len(sgs_blocks)} séries.")
    except Exception as e:
        print(f"[aviso] SGS falhou ({e}) - data.json sai sem dados do SGS")
        sgs_blocks = []

    # Últimos trimestres do IF.data calculados automaticamente (considera a
    # defasagem de publicação de ~75 dias), olhando pra trás o suficiente
    # pra dar histórico de verdade nos cards (mesma janela de 6 trimestres
    # já usada pro Ranking de Reclamações) em vez de só o trimestre mais
    # recente.
    quarters = quarters_ifdata_recentes(6)
    try:
        ifdata_blocks = build_ifdata_block(quarters)
        print(f"[sucesso] IF.data processado com {len(ifdata_blocks)} blocos (trimestres: {quarters}).")
    except Exception as e:
        print(f"[aviso] IF.data falhou ({e}) - data.json segue sem dados de carteira")

        if DEBUG:
            import json as _json
            import requests
            from urllib.parse import quote, quote_plus

            def _testar(label, url):
                print(f"[debug-raw] tentativa '{label}': GET {url}")
                try:
                    r = requests.get(url, headers=headers, timeout=30)
                    print(f"[debug-raw]   status={r.status_code} content_type={r.headers.get('content-type')}")
                    print(f"[debug-raw]   body[:300]={r.text[:300]!r}")
                    if r.status_code == 200:
                        corpo = r.text.strip()
                        if corpo.startswith("/*") and corpo.endswith("*/"):
                            corpo_limpo = corpo[2:-2].strip()
                            try:
                                _json.loads(corpo_limpo)
                                print(f"[debug-raw]   -> 200 OK, JSON embrulhado em /*...*/ "
                                      f"(conteúdo válido). ESSA é a tentativa que funciona.")
                            except Exception as e3:
                                print(f"[debug-raw]   -> 200 mas wrapper /*...*/ com conteúdo inválido: {e3}")
                        else:
                            try:
                                _json.loads(corpo)
                                print(f"[debug-raw]   -> 200 OK, JSON puro sem wrapper. "
                                      f"ESSA é a tentativa que funciona.")
                            except Exception as e3:
                                print(f"[debug-raw]   -> 200 mas corpo não é JSON reconhecível: {e3}")
                except Exception as e2:
                    print(f"[debug-raw]   falha até no request cru: {e2}")

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            base = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata/IfDataCadastro"
            for anomes in quarters:
                # Tentativa 1: "$" literal na chave, valor com espaço -> %20
                _testar("$filter literal + %20", f"{base}?$filter={quote(f'AnoMes eq {anomes}')}&$top=3&$format=json")
                # Tentativa 2: "$" literal na chave, valor com espaço -> + (quote_plus)
                _testar("$filter literal + '+'", f"{base}?$filter={quote_plus(f'AnoMes eq {anomes}')}&$top=3&$format=json")
                # Tentativa 3: sem $filter nenhum - só pra saber se o endpoint em si
                # responde OK sem filtro (isola se o problema é o $filter ou é mais básico)
                _testar("sem $filter", f"{base}?$top=3&$format=json")
        ifdata_blocks = []

    # Últimos trimestres para o ranking de reclamações, calculados
    # automaticamente (com defasagem de publicação) em vez de hardcoded.
    periodos_reclamacoes = periodos_reclamacoes_recentes(6)
    try:
        reclamacoes_blocks, periodos_ok = build_reclamacoes_block(periodos_reclamacoes)
        if set(periodos_ok) != set(periodos_reclamacoes):
            faltando = sorted(set(periodos_reclamacoes) - set(periodos_ok))
            print(f"[aviso] {len(faltando)} período(s) solicitado(s) não retornaram dado: {faltando} "
                  f"- provavelmente ainda não publicados pelo Bacen")
        print(f"[sucesso] Reclamações processadas com {len(reclamacoes_blocks)} blocos "
              f"(períodos com dado: {periodos_ok}).")
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
