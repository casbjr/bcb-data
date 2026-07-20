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
    _padrao_termos,
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


def identificar_bancos_alvo(nome_instituicao: str) -> list[str]:
    """Como identificar_tier(), mas retorna TODAS as chaves de BANCOS_ALVO que
    baterem no nome (em vez de só a primeira) - necessário pra detectar o
    caso de conglomerados que juntam dois bancos-alvo numa linha só
    (ex.: BTG Pactual/Banco Pan) sem perder um deles silenciosamente.
    Usa a mesma busca com fronteira de palavra de identificar_tier() -
    necessário porque o Ranking de Reclamações usa nomes curtos ("INTER",
    "BV", "ITAU") que só são seguros como termo de busca com \\b."""
    nome_upper = str(nome_instituicao).upper()
    return sorted(
        chave for chave, banco in BANCOS_ALVO.items()
        if re.search(_padrao_termos(banco["termos"]), nome_upper)
    )


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
        return [], []

    col_instituicao = next((c for c in df.columns if "institui" in c.lower()), None)
    col_indice = next((c for c in df.columns if "indice" in c.lower() or "índice" in c.lower()), None)
    if col_instituicao is None or col_indice is None:
        print(f"[aviso] colunas não identificadas no ranking - colunas: {list(df.columns)}")
        return [], []

    # Períodos que de fato vieram com dado (podem ser menos que os
    # solicitados, se algum trimestre ainda não tiver sido publicado).
    periodos_ok = sorted(df[["Ano", "Periodo"]].drop_duplicates().itertuples(index=False, name=None))

    # Agrupa por chave canônica do banco-alvo (não pelo nome literal do
    # Bacen) - o mesmo banco pode aparecer com grafias diferentes em
    # trimestres diferentes (ex.: "C6 BANK (conglomerado)" vs "BANCO C6
    # (conglomerado)"), e agrupar pelo texto puro fragmentava a série em
    # cards duplicados em vez de um histórico contínuo por banco.
    df = df.copy()
    df["bancos_chave"] = df[col_instituicao].apply(identificar_bancos_alvo)
    df["chave_grupo"] = df["bancos_chave"].apply(lambda l: "+".join(l) if l else None)
    sem_chave = df[df["chave_grupo"].isna()]
    if not sem_chave.empty:
        print(f"[aviso] {len(sem_chave)} linha(s) do ranking bateram no filtro de termos mas não "
              f"resolveram pra nenhuma chave de BANCOS_ALVO: "
              f"{sem_chave[col_instituicao].unique().tolist()}")
    df = df.dropna(subset=["chave_grupo"])

    # Converte strings BR para float americano; valores que não derem pra
    # converter são DESCARTADOS (não viram 0 - um índice de reclamações "0"
    # entraria no ranking como o melhor resultado possível, o que seria
    # enganoso se na verdade é só um erro de parsing).
    df["valor_num"] = pd.to_numeric(
        df[col_indice].astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
        errors="coerce"
    )
    invalidos = df[df["valor_num"].isna()]
    if not invalidos.empty:
        print(f"[aviso] {len(invalidos)} valor(es) de índice não-numérico descartado(s): "
              f"{list(zip(invalidos[col_instituicao], invalidos[col_indice]))}")
    df = df.dropna(subset=["valor_num"])

    blocks = []
    for chave_grupo, grupo in df.groupby("chave_grupo"):
        grupo = grupo.sort_values(["Ano", "Periodo"])
        chaves = chave_grupo.split("+")

        if len(chaves) > 1:
            # Conglomerado que junta mais de um banco-alvo numa linha só
            # (ex.: BTG comprou o Pan e o Bacen passou a publicar os dois
            # como uma entidade). Mantemos como um card único em vez de
            # silenciosamente atribuir a linha inteira ao tier de só um
            # dos dois bancos.
            label_base = " / ".join(NOME_CANONICO.get(c, c) for c in chaves)
            tier = "conglomerado"
        else:
            label_base = NOME_CANONICO.get(chaves[0], chaves[0])
            tier = BANCOS_ALVO[chaves[0]]["tier"]

        ultimo_valor = float(grupo["valor_num"].iloc[-1])
        blocks.append({
            "key": chave_grupo,
            "label": f"{label_base} · índice de reclamações",
            "unit": "pontos",
            "group": label_base,
            "tier": tier,
            "dates": [f"{p}Q{str(a)[2:]}" for a, p in zip(grupo["Ano"], grupo["Periodo"])],
            "values": [round(float(v), 2) for v in grupo["valor_num"]],
            "_ultimo_valor": ultimo_valor,
        })

    # Ranking Top 10: ordena pelo índice mais recente, do pior (maior) pro
    # melhor (menor) - é assim que o ranking de reclamações do Bacen é lido
    # convencionalmente - e numera a posição de cada card.
    blocks.sort(key=lambda b: b["_ultimo_valor"], reverse=True)
    for i, block in enumerate(blocks, start=1):
        block["rank"] = i
        del block["_ultimo_valor"]

    bancos_sem_dado = sorted(set(BANCOS_ALVO) - {c for chave in df["chave_grupo"].unique() for c in chave.split("+")})
    if bancos_sem_dado:
        print(f"[aviso] banco(s)-alvo sem nenhuma entrada no ranking de reclamações nos períodos "
              f"{periodos}: {bancos_sem_dado} - confira se o nome oficial no arquivo do Bacen bate "
              f"com os termos em BANCOS_ALVO (rode listar_instituicoes_alvo() ou inspecione "
              f"df[col_instituicao].unique() pra achar o nome certo).")

    return blocks, periodos_ok


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
