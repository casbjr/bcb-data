"""
Pipeline de dados de cartão de crédito - Banco Central do Brasil
==================================================================

Cinco fontes:
  1. SGS        -> séries mensais, nível Sistema Financeiro Nacional (agregado)
  2. IF.data    -> dados trimestrais POR INSTITUIÇÃO (Porto + concorrentes
                    diretos: Pan, BV, Inter, C6 + benchmarks: Itaú, Bradesco,
                    Santander, BTG, Nubank)
  3. SCR.data   -> mensal, mercado todo, muito granular (não isola banco por nome)
  4. Meios de Pagamentos (MPV) -> trimestral, volume/quantidade de transações com cartão
  5. Ranking de Reclamações -> trimestral, POR INSTITUIÇÃO (proxy de qualidade
     operacional - cartão de crédito costuma liderar as reclamações)

Requer: pip install -r requirements.txt --break-system-packages
        (pina python-bcb==0.3.3 - usado só pro SGS agora. O IF.data foi
        migrado pra chamar a API OData do Bacen direto via requests, sem
        passar pela lib - depois de várias rodadas de diagnóstico ficou
        claro que valia mais a pena ver a resposta crua do servidor do
        que continuar adivinhando o comportamento interno da lib pra
        esse serviço específico. Ver _ifdata_get().)

Uso:
  python bacen_cartao_pipeline.py

Saída (em ./output/):
  sgs_cartao_mensal.csv
  ifdata_cartao_trimestral.csv
  scr_data_cartao_<ano_mes>.csv      (opcional, desligado por padrão - ver fim do arquivo)
  meios_pagamento_trimestral.csv     (opcional, desligado por padrão - ver fim do arquivo)
"""

import io
import json
import zipfile
import requests
import os
import time
from datetime import date
from urllib.parse import quote
import pandas as pd
from bcb import sgs  # IF.data vai direto via requests (ver _ifdata_get) - a lib
                      # pinada em 0.3.3 não tá confiável pra esse serviço

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. SGS - séries mensais nacionais de cartão de crédito
# ---------------------------------------------------------------------------

SGS_SERIES_CARTAO = {
    "inadimplencia_total_sfn": 21082,                 # NPL >90d, total SFN
    "inadimplencia_cartao_total_pf": 21129,            # NPL >90d, cartão PF (rotativo+parcelado)
    "inadimplencia_cartao_rotativo_pf": 21127,         # NPL >90d, rotativo PF
    "inadimplencia_cartao_rotativo_pj": 21104,         # NPL >90d, rotativo PJ
    "inadimplencia_cartao_parcelado_pj": 21105,        # NPL >90d, parcelado PJ
    "juros_cartao_rotativo_pf": 22022,                 # taxa média de juros % a.m., rotativo PF
    "juros_cartao_total_pf": 22024,                    # taxa média de juros % a.m., cartão total PF
    "juros_cartao_rotativo_pj": 22019,                 # taxa média de juros % a.m., rotativo PJ
    "saldo_cartao_total_pf": 20590,                    # saldo carteira, cartão total PF (R$ milhões)
    "saldo_cartao_rotativo_pf": 20572,                 # saldo carteira, rotativo PF (R$ milhões)
    "saldo_cartao_parcelado_pf": 20588,                # saldo carteira, parcelado PF (R$ milhões)
    "saldo_cartao_total_pj": 20564,                    # saldo carteira, cartão total PJ (R$ milhões)
    "saldo_cartao_rotativo_pj": 20561,                 # saldo carteira, rotativo PJ (R$ milhões)
    "saldo_cartao_parcelado_pj": 20562,                # saldo carteira, parcelado PJ (R$ milhões)
    "inadimplencia_cartao_parcelado_pf": 21128,        # NPL >90d, parcelado PF (fechava a matriz PF)
    "juros_cartao_parcelado_pf": 22023,                # taxa média de juros % a.m., parcelado PF
    "juros_cartao_parcelado_pj": 22020,                # taxa média de juros % a.m., parcelado PJ
    "saldo_carteira_total_sfn": 20539,                 # saldo total SFN, todos os produtos - denominador pra market share
}


def _com_retry(fn, tentativas: int = 3, espera_inicial: float = 5.0, label: str = "chamada"):
    """Executa fn() com retry e backoff exponencial simples. Uso pontual pra
    chamadas de rede instáveis (o Bacen tem hora que soluça, tipo o
    'Download error: code = 22019' que já vimos no SGS) - não é pra
    mascarar erro de verdade (URL errada, filtro malformado etc.), só pra
    não deixar uma instabilidade de alguns segundos derrubar a rodada
    inteira do Actions."""
    ultimo_erro = None
    espera = espera_inicial
    for tentativa in range(1, tentativas + 1):
        try:
            return fn()
        except Exception as e:
            ultimo_erro = e
            if tentativa < tentativas:
                print(f"[aviso] {label} falhou na tentativa {tentativa}/{tentativas} ({e}) "
                      f"- tentando de novo em {espera:.0f}s")
                time.sleep(espera)
                espera *= 2
    raise ultimo_erro


def get_sgs_cartao(start: str = "2024-01-01") -> pd.DataFrame:
    """Baixa as séries mensais de cartão do SGS a partir de `start` (YYYY-MM-DD)."""
    df = _com_retry(lambda: sgs.get(SGS_SERIES_CARTAO, start=start), label="SGS")
    df.index.name = "data"
    return df


# ---------------------------------------------------------------------------
# 2. IF.data - dados trimestrais por instituição (Porto, Itaú, Nubank)
# ---------------------------------------------------------------------------

# Termos de busca no cadastro do IF.data. Ajuste se o nome oficial
# divergir (ex.: PortoBank pode estar registrado sob outra razão social
# - confirme rodando `listar_instituicoes_alvo()` abaixo antes de usar
# em produção).
#
# "tier":
#   concorrente -> concorrentes diretos de porte/perfil parecido com o
#                  PortoBank (cartão + crédito ao consumidor, porte médio)
#   benchmark   -> bancões/gigantes digitais usados como referência
#                  aspiracional, não como comparação direta de porte
#
# ATENÇÃO regex: os termos abaixo são usados como substring (via
# str.contains), sem borda de palavra. Por isso os termos precisam ser
# específicos o suficiente pra não bater com palavras que só contêm a
# mesma sequência de letras no meio - já vimos isso acontecer na prática:
# "ITA" batia com "FACILITA IP", "CAPITAL" e "DIGITAL" (todas contêm
# "ita" no meio da palavra); "PORTO " batia com "BANCO PORTO REAL DE
# INVEST.S.A", um banco completamente diferente da Porto Seguro. Prefira
# sempre termos de 2+ palavras ou nomes completos em vez de fragmentos
# curtos. SEMPRE rode listar_instituicoes_alvo() antes de confiar num
# trimestre novo, pra conferir se não entrou nada indesejado.
BANCOS_ALVO = {
    "porto":     {"termos": ["PORTO SEGURO", "PORTO BANK"], "tier": "concorrente"},
    "pan":       {"termos": ["BANCO PAN"], "tier": "concorrente"},
    "bv":        {"termos": ["BANCO VOTORANTIM", "BV FINANCEIRA"], "tier": "concorrente"},
    "inter":     {"termos": ["BANCO INTER"], "tier": "concorrente"},
    "c6":        {"termos": ["C6 BANK", "BANCO C6"], "tier": "concorrente"},
    "itau":      {"termos": ["ITAÚ UNIBANCO", "ITAU UNIBANCO"], "tier": "benchmark"},  # nome completo evita falso-positivo em palavras como "capital", "digital", "facilita"
    "bradesco":  {"termos": ["BRADESCO"], "tier": "benchmark"},
    "santander": {"termos": ["SANTANDER"], "tier": "benchmark"},
    "btg":       {"termos": ["BTG PACTUAL"], "tier": "benchmark"},
    "nubank":    {"termos": ["NU PAGAMENTOS", "NU FINANCEIRA"], "tier": "benchmark"},
}


def _todos_termos() -> list[str]:
    return [t for banco in BANCOS_ALVO.values() for t in banco["termos"]]


def identificar_tier(nome_instituicao: str) -> str:
    """Dado um NomeInstituicao do cadastro do Bacen, identifica em qual
    banco-alvo ele bateu e retorna o tier ('concorrente' ou 'benchmark').
    Retorna 'outro' se não bater com nenhum termo (não deveria acontecer
    se a linha já passou pelo filtro de padrao)."""
    nome_upper = str(nome_instituicao).upper()
    for banco in BANCOS_ALVO.values():
        if any(termo.upper() in nome_upper for termo in banco["termos"]):
            return banco["tier"]
    return "outro"

# Relatório 11 = Carteira de crédito ativa Pessoa Física - modalidade e
# prazo de vencimento. É aqui que aparece a linha de "Cartão de Crédito"
# por instituição. TipoInstituicao=1 (conglomerado prudencial) costuma
# ser o nível mais comparável entre bancos grandes.
RELATORIO_CARTAO_PF = "11"
TIPO_INSTITUICAO = 1


IFDATA_BASE_URL = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"


def _ifdata_get(endpoint: str, filtro: str = None, top: int = None) -> list[dict]:
    """Chama um endpoint do IFDATA direto via requests, sem passar pelo
    python-bcb - depois de várias rodadas de diagnóstico (erro de parsing,
    join de código que não batia, 44 mil linhas suspeitas de filtro
    ignorado), decidimos parar de adivinhar o comportamento da lib pinada
    em 0.3.3 e falar com a API na mão, onde dá pra ver a resposta crua.

    ATENÇÃO: a sintaxe exata do $filter combinando múltiplos campos ainda
    não foi validada contra o servidor real (sem acesso de rede aqui pra
    testar) - se dar 400, o corpo do erro (que a gente já sabe descascar)
    vai dizer o que o Bacen não gostou.
    """
    url = f"{IFDATA_BASE_URL}/{endpoint}"
    partes = []
    if filtro:
        # "$filter" literal na chave (não deixar o requests re-encodar pra
        # %24filter, isso já deu 400 "malformed" numa rodada anterior);
        # só o VALOR do filtro é urlencoded.
        partes.append(f"$filter={quote(filtro)}")
    if top:
        partes.append(f"$top={top}")
    partes.append("$format=json")
    url_completa = f"{url}?{'&'.join(partes)}"

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp = requests.get(url_completa, headers=headers, timeout=60)

    corpo = resp.text.strip()
    if corpo.startswith("/*") and corpo.endswith("*/"):
        corpo = corpo[2:-2].strip()

    if resp.status_code != 200:
        raise RuntimeError(f"IFDATA {endpoint} devolveu {resp.status_code} pra "
                            f"GET {url_completa} - corpo: {corpo[:400]}")

    dados = json.loads(corpo)
    if isinstance(dados, dict):
        return dados.get("value", [])
    return dados if isinstance(dados, list) else []


def listar_instituicoes_alvo(anomes: int) -> pd.DataFrame:
    """Função de apoio: lista o que o cadastro do Bacen tem para os termos
    de busca, para você confirmar o nome oficial antes de automatizar."""
    registros = _ifdata_get("IfDataCadastro", filtro=f"AnoMes eq {anomes}")
    cadastro = pd.DataFrame(registros)
    if cadastro.empty:
        return cadastro

    padrao = "|".join(_todos_termos())
    resultado = cadastro[cadastro["NomeInstituicao"].str.contains(padrao, case=False, na=False)][
        ["CodInst", "NomeInstituicao", "Td", "CodConglomeradoPrudencial"]
    ].copy()
    resultado["tier"] = resultado["NomeInstituicao"].apply(identificar_tier)
    return resultado


def get_quarters(year: int, defasagem_dias: int = 75) -> list[int]:
    """Gera as data-base trimestrais (AAAAMM) de um ano que já devem estar
    publicadas, considerando a defasagem do IF.data (60-90 dias após o
    fechamento do trimestre).

    Ex.: hoje=10/jul/2026 -> 1T26 fechou 31/mar, +75d = 14/jun -> já
    publicado, entra na lista. 2T26 fecha 30/jun, +75d = 13/set -> ainda
    não, fica de fora.
    """
    hoje = date.today()
    candidatos = [(year, m) for m in (3, 6, 9, 12)]
    publicados = []
    for ano, mes_fim in candidatos:
        # data aproximada de fechamento do trimestre
        fechamento = date(ano, mes_fim, 28)
        dias_desde_fechamento = (hoje - fechamento).days
        if dias_desde_fechamento >= defasagem_dias:
            publicados.append(ano * 100 + mes_fim)
    return publicados


def get_ifdata_cartao(anomes_list: list[int]) -> pd.DataFrame:
    """Busca a linha 'Cartão de Crédito' do relatório 11 (PF) do IF.data
    pros bancos-alvo (concorrentes diretos + benchmarks), nos trimestres
    informados."""
    resultados = []
    for anomes in anomes_list:
        registros_cadastro = _ifdata_get("IfDataCadastro", filtro=f"AnoMes eq {anomes}")
        cadastro = pd.DataFrame(registros_cadastro)
        if cadastro.empty:
            print(f"[aviso] cadastro veio vazio pra {anomes}")
            continue

        padrao = "|".join(_todos_termos())
        alvo = cadastro[cadastro["NomeInstituicao"].str.contains(padrao, case=False, na=False)].copy()
        if alvo.empty:
            print(f"[aviso] nenhuma instituição-alvo encontrada no cadastro de {anomes}")
            continue
        alvo["tier"] = alvo["NomeInstituicao"].apply(identificar_tier)

        # Mapa código -> (nome, tier) usando CodInst E CodConglomeradoPrudencial
        # como chave possível, cobrindo os dois cenários sem adivinhar qual
        # dos dois o relatório de valores realmente usa.
        mapa_codigo = {}
        for _, linha in alvo.iterrows():
            info = (linha["NomeInstituicao"], linha["tier"])
            if pd.notna(linha.get("CodInst")):
                mapa_codigo[str(linha["CodInst"])] = info
            if "CodConglomeradoPrudencial" in alvo.columns and pd.notna(linha.get("CodConglomeradoPrudencial")):
                mapa_codigo[str(linha["CodConglomeradoPrudencial"])] = info
        codigos_alvo = list(mapa_codigo.keys())

        # Filtro combinado direto no $filter da API - ainda não validado
        # contra o servidor real. Relatorio como string entre aspas simples
        # (convenção OData pra campo texto); se o Bacen tratar como
        # numérico, o erro 400 vai apontar isso.
        filtro_valores = (f"AnoMes eq {anomes} and TipoInstituicao eq {TIPO_INSTITUICAO} "
                           f"and Relatorio eq '{RELATORIO_CARTAO_PF}'")
        registros_valores = _ifdata_get("IfDataValores", filtro=filtro_valores)
        dados = pd.DataFrame(registros_valores)
        if dados.empty:
            print(f"[aviso] relatório {RELATORIO_CARTAO_PF} vazio para {anomes} "
                  f"(TipoInstituicao={TIPO_INSTITUICAO}) - tente TipoInstituicao=2 ou 3, "
                  f"ou confira se o $filter combinado é aceito pelo Bacen")
            continue

        dados_antes = len(dados)
        print(f"[aviso] {anomes}: colunas retornadas pelo relatório de valores: {list(dados.columns)}")
        print(f"[aviso] {anomes}: {dados_antes} linha(s) retornadas já com o filtro "
              f"combinado no servidor (antes tínhamos 44 mil linhas sem filtro nenhum - "
              f"se esse número ainda estiver na casa dos milhares, o filtro continua não "
              f"colando; se estiver pequeno/plausível, colou).")

        codinst_amostra_dados = sorted(dados["CodInst"].astype(str).dropna().unique().tolist())[:10]
        dados = dados[dados["CodInst"].astype(str).isin(codigos_alvo)].copy()
        if dados.empty:
            print(f"[aviso] {anomes}: cadastro achou {len(alvo)} instituição(ões) "
                  f"(códigos tentados - CodInst e CodConglomeradoPrudencial: {codigos_alvo[:10]}), "
                  f"relatório {RELATORIO_CARTAO_PF} veio com {dados_antes} linha(s) no total "
                  f"(amostra de CodInst no relatório: {codinst_amostra_dados}), mas NENHUMA bateu.")
            continue
        dados["NomeInstituicao"] = dados["CodInst"].astype(str).map(lambda c: mapa_codigo[c][0])
        dados["tier"] = dados["CodInst"].astype(str).map(lambda c: mapa_codigo[c][1])
        dados["AnoMes"] = anomes
        resultados.append(dados)

    if not resultados:
        return pd.DataFrame()

    df = pd.concat(resultados, ignore_index=True)

    # IMPORTANTE: confira os valores reais de NomeColuna antes de filtrar
    # em produção - o texto exato pode variar de período para período.
    # Rode: df['NomeColuna'].unique() para ver as opções e ajustar o filtro.
    # Usa "Cartão" (com acento) em vez de "Cart" pra evitar casar outras
    # colunas que por acaso comecem com essas letras.
    mask_cartao = df["NomeColuna"].str.contains("Cartão", case=False, na=False)
    if not mask_cartao.any():
        # fallback pro texto sem acento, caso a API normalize diferente
        mask_cartao = df["NomeColuna"].str.contains("Cartao", case=False, na=False)
    if not mask_cartao.any():
        # Nem "Cartão" nem "Cartao" bateram - antes isso retornava vazio
        # SEM avisar por quê, o que é pior que um erro (parece que deu
        # tudo certo, só que sem carteira nenhuma). Agora mostra as
        # colunas que o relatório realmente trouxe, pra ajustar o texto
        # do filtro (ou o RELATORIO_CARTAO_PF / TIPO_INSTITUICAO, se o
        # relatório 11 nem tiver a modalidade cartão nesse recorte).
        colunas_disponiveis = sorted(df["NomeColuna"].dropna().unique().tolist())
        print(f"[aviso] nenhuma coluna com 'Cartão'/'Cartao' encontrada no relatório "
              f"{RELATORIO_CARTAO_PF} (TipoInstituicao={TIPO_INSTITUICAO}). "
              f"Colunas disponíveis ({len(colunas_disponiveis)}): {colunas_disponiveis}")
        return pd.DataFrame()
    return df[mask_cartao]


# ---------------------------------------------------------------------------
# 3. SCR.data - mercado todo, mensal, MUITO granular (não isola banco por nome)
# ---------------------------------------------------------------------------
#
# Não é uma API tipo SGS/IF.data - é um arquivo .ZIP por ano (todos os meses
# dentro), baixado direto do site do Bacen. Dentro tem ~700 mil séries:
# cruza modalidade de crédito x UF x PF/PJ x renda x indexador, etc.
# Detalha por SEGMENTO da instituição (S1, S2...), não pelo nome do banco -
# ou seja, não dá pra isolar Porto/Itaú/Nubank aqui.

SCR_DATA_URL_TEMPLATE = "https://www.bcb.gov.br/pda/desig/scrdata_{ano}.zip"


def baixar_scr_data(ano: int, pasta_destino: str = None) -> str:
    """Baixa e extrai o ZIP anual do SCR.data. Retorna o caminho da pasta
    extraída. Atenção: arquivo grande (pode levar minutos)."""
    pasta_destino = pasta_destino or os.path.join(OUTPUT_DIR, f"scr_data_{ano}")
    os.makedirs(pasta_destino, exist_ok=True)

    url = SCR_DATA_URL_TEMPLATE.format(ano=ano)
    print(f"Baixando {url} ...")
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        z.extractall(pasta_destino)

    return pasta_destino


def carregar_scr_data_cartao(pasta_extraida: str, ano_mes: str) -> pd.DataFrame:
    """Lê o CSV de um mês específico (formato AAAAMM) dentro da pasta
    extraída e filtra as linhas de modalidade relacionadas a cartão.

    O nome exato do arquivo e das colunas pode variar por versão do
    SCR.data (v1 vs v2) - confira a Metodologia (Versão 2) antes de usar
    em produção: ver link em SCR_DATA_METODOLOGIA_URL abaixo.
    """
    candidatos = [f for f in os.listdir(pasta_extraida) if ano_mes in f and f.endswith(".csv")]
    if not candidatos:
        raise FileNotFoundError(f"Nenhum CSV encontrado para {ano_mes} em {pasta_extraida}")

    caminho = os.path.join(pasta_extraida, candidatos[0])
    # SCR.data costuma vir com separador ';' e encoding latin-1
    df = pd.read_csv(caminho, sep=";", encoding="latin-1", low_memory=False)

    # Ajuste o nome da coluna de modalidade conforme o cabeçalho real
    # (confira df.columns antes de rodar em produção).
    col_modalidade = next((c for c in df.columns if "modalidade" in c.lower()), None)
    if col_modalidade is None:
        print("[aviso] coluna de modalidade não encontrada automaticamente - "
              "inspecione df.columns manualmente")
        return df

    return df[df[col_modalidade].str.contains("Cart", case=False, na=False)]


SCR_DATA_METODOLOGIA_URL = "https://www.bcb.gov.br/pda/desig/metodologia_versao2.pdf"


# ---------------------------------------------------------------------------
# 4. Meios de Pagamentos - volumetria/quantidade de transações com cartão
#    (trimestral, nível mercado - bom complemento, não isola banco)
# ---------------------------------------------------------------------------

MPV_ENDPOINT = (
    "https://olinda.bcb.gov.br/olinda/servico/MPV_DadosAbertos/versao/v1/odata/"
    "MeiosdePagamentosTrimestralDA"
)


def get_meios_pagamento_cartao(formato: str = "json") -> pd.DataFrame:
    """Puxa a série trimestral de Meios de Pagamento (cartões de crédito e
    débito, boletos, TED/transferências). Disponível 90 dias após o
    fechamento do trimestre. Ajuste $filter conforme os campos retornados -
    rode uma vez sem filtro para inspecionar as colunas disponíveis."""
    url = f"{MPV_ENDPOINT}?$format={formato}"
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    registros = data.get("value", data)
    return pd.DataFrame(registros)


# ---------------------------------------------------------------------------
# 5. Ranking de Reclamações - por instituição, trimestral
# ---------------------------------------------------------------------------
#
# Cartão de crédito costuma ser a categoria #1 de reclamação nesse ranking,
# então mesmo não sendo um dado "de cartão" em si, é um proxy forte de
# qualidade operacional em cartão, comparável entre Porto/Itaú/Nubank.

RANKING_RECLAMACOES_URL = "https://www3.bcb.gov.br/rdrweb/rest/ext/ranking/arquivo"


def baixar_ranking_reclamacoes(ano: int, periodo: int, periodicidade: str = "TRIMESTRAL",
                                 tipo: str = "Bancos e financeiras") -> pd.DataFrame:
    """Baixa o CSV do ranking de reclamações de um trimestre específico.

    periodo: 1 a 4 (trimestre) quando periodicidade='TRIMESTRAL'.

    Atenção: não consegui validar os nomes exatos das colunas contra o
    arquivo real (sem rede no ambiente onde escrevi isso) - o código abaixo
    detecta as colunas por palavra-chave ao invés de nome fixo. Rode
    `df.columns` na primeira vez e ajuste os termos de busca se precisar.
    """
    params = {"ano": ano, "periodicidade": periodicidade, "periodo": periodo, "tipo": tipo}
    resp = requests.get(RANKING_RECLAMACOES_URL, params=params, timeout=60)
    resp.raise_for_status()

    # Arquivos do Bacen costumam vir em latin-1 com separador ';' - se vier
    # diferente, o pandas geralmente detecta sozinho com engine='python'.
    try:
        df = pd.read_csv(io.BytesIO(resp.content), sep=";", encoding="latin-1", engine="python")
        print(f"[debug] colunas do ranking de reclamações ({ano}T{periodo}): {list(df.columns)}")
    except Exception:
        df = pd.read_csv(io.BytesIO(resp.content), sep=None, encoding="latin-1", engine="python")

    return df


def get_ranking_reclamacoes_cartao(periodos: list[tuple[int, int]]) -> pd.DataFrame:
    """Busca o ranking de reclamações para Porto, Itaú e Nubank nos
    (ano, trimestre) informados, ex.: [(2025, 3), (2025, 4), (2026, 1)]."""
    resultados = []
    for ano, periodo in periodos:
        try:
            df = baixar_ranking_reclamacoes(ano, periodo)
        except Exception as e:
            print(f"[aviso] falha ao baixar ranking {ano}T{periodo}: {e}")
            continue

        col_instituicao = next((c for c in df.columns if "institui" in c.lower()), None)
        if col_instituicao is None:
            print(f"[aviso] coluna de instituição não encontrada em {ano}T{periodo} - "
                  f"colunas disponíveis: {list(df.columns)}")
            continue

        padrao = "|".join(_todos_termos())
        alvo = df[df[col_instituicao].astype(str).str.contains(padrao, case=False, na=False)].copy()
        if alvo.empty:
            nomes_unicos = df[col_instituicao].dropna().unique().tolist()
            print(f"[aviso] nenhum banco-alvo bateu em {ano}T{periodo} (coluna='{col_instituicao}') - "
                  f"nomes disponíveis nesse arquivo: {nomes_unicos}")
            continue

        alvo["tier"] = alvo[col_instituicao].apply(identificar_tier)
        alvo["Ano"] = ano
        alvo["Periodo"] = periodo
        alvo["AnoPeriodo"] = f"{ano}T{periodo}"
        resultados.append(alvo)

    if not resultados:
        return pd.DataFrame()
    return pd.concat(resultados, ignore_index=True)


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Baixando séries SGS (nacional, mensal)...")
    sgs_df = get_sgs_cartao(start="2024-01-01")
    sgs_path = os.path.join(OUTPUT_DIR, "sgs_cartao_mensal.csv")
    sgs_df.to_csv(sgs_path)
    print(f"  -> salvo em {sgs_path} ({len(sgs_df)} linhas)")

    ano_atual = date.today().year
    quarters_atuais = get_quarters(ano_atual) or get_quarters(ano_atual - 1)[-1:]

    print(f"\nConferindo nomes de instituições no cadastro IF.data ({quarters_atuais[-1]})...")
    print(listar_instituicoes_alvo(quarters_atuais[-1]))

    print(f"\nBaixando IF.data trimestral (Porto, Itaú, Nubank) para {quarters_atuais}...")
    ifdata_df = get_ifdata_cartao(quarters_atuais)
    if not ifdata_df.empty:
        ifdata_path = os.path.join(OUTPUT_DIR, "ifdata_cartao_trimestral.csv")
        ifdata_df.to_csv(ifdata_path, index=False)
        print(f"  -> salvo em {ifdata_path} ({len(ifdata_df)} linhas)")
    else:
        print("  -> nenhum dado retornado, revise BANCOS_ALVO / TIPO_INSTITUICAO / Relatorio")

    # --- Fontes extras (mercado todo, não isolam banco por nome) ---
    # Desligadas por padrão (SCR.data é arquivo grande e demorado).
    # Ative manualmente quando precisar:
    #
    # print("\nBaixando SCR.data 2026 (arquivo grande, pode demorar)...")
    # pasta_scr = baixar_scr_data(2026)
    # scr_cartao = carregar_scr_data_cartao(pasta_scr, ano_mes="202603")
    # scr_cartao.to_csv(os.path.join(OUTPUT_DIR, "scr_data_cartao_202603.csv"), index=False)
    #
    # print("\nBaixando Meios de Pagamentos trimestral...")
    # mpv_df = get_meios_pagamento_cartao()
    # mpv_df.to_csv(os.path.join(OUTPUT_DIR, "meios_pagamento_trimestral.csv"), index=False)
    # print(f"  -> {len(mpv_df)} linhas - inspecione as colunas pra filtrar cartão de crédito")
