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
        passar pela lib. Ver _ifdata_get().)

Uso:
  python bacen_cartao_pipeline.py

Saída (em ./output/):
  sgs_cartao_mensal.csv
  ifdata_cartao_trimestral.csv
"""

import io
import json
import re
import zipfile
import requests
import os
import time
from datetime import date
from urllib.parse import quote
import pandas as pd
from bcb import sgs

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
    "inadimplencia_cartao_parcelado_pf": 21128,        # NPL >90d, parcelado PF
    "juros_cartao_parcelado_pf": 22023,                # taxa média de juros % a.m., parcelado PF
    "juros_cartao_parcelado_pj": 22020,                # taxa média de juros % a.m., parcelado PJ
    "saldo_carteira_total_sfn": 20539,                 # saldo total SFN
}


def _com_retry(fn, tentativas: int = 3, espera_inicial: float = 5.0, label: str = "chamada"):
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
    df = _com_retry(lambda: sgs.get(SGS_SERIES_CARTAO, start=start), label="SGS")
    df.index.name = "data"
    return df


# ---------------------------------------------------------------------------
# 2. IF.data - dados trimestrais por instituição
# ---------------------------------------------------------------------------

# O IF.data usa a razão social completa (ex.: "ITAÚ UNIBANCO S.A."), mas o
# Ranking de Reclamações usa nomes curtos/de marca pro mesmo banco (ex.: só
# "ITAU", só "INTER") - por isso cada banco-alvo tem termos das duas
# convenções. Termos curtos ("INTER", "BV", "ITAU") só são seguros porque a
# busca usa fronteira de palavra (ver _padrao_termos) - sem isso, "INTER"
# bateria como substring em "BANCO INTERMEDIUM", um banco de verdade e
# completamente diferente do Inter.
BANCOS_ALVO = {
    "porto":     {"termos": ["PORTO SEGURO", "PORTO BANK"], "tier": "concorrente"},
    "pan":       {"termos": ["BANCO PAN"], "tier": "concorrente"},
    "bv":        {"termos": ["BANCO VOTORANTIM", "BV FINANCEIRA", "BV"], "tier": "concorrente"},
    "inter":     {"termos": ["BANCO INTER", "INTER"], "tier": "concorrente"},
    "c6":        {"termos": ["C6 BANK", "BANCO C6"], "tier": "concorrente"},
    "itau":      {"termos": ["ITAÚ UNIBANCO", "ITAU UNIBANCO", "ITAÚ", "ITAU"], "tier": "benchmark"},
    "bradesco":  {"termos": ["BRADESCO"], "tier": "benchmark"},
    "santander": {"termos": ["SANTANDER"], "tier": "benchmark"},
    "btg":       {"termos": ["BTG PACTUAL"], "tier": "benchmark"},
    "nubank":    {"termos": ["NU PAGAMENTOS", "NU FINANCEIRA"], "tier": "benchmark"},
}


def _todos_termos() -> list[str]:
    return [t for banco in BANCOS_ALVO.values() for t in banco["termos"]]


def _padrao_termos(termos: list[str]) -> str:
    """Monta um padrão regex com fronteira de palavra (\\b) pra cada termo -
    evita que termos curtos batam como substring dentro de nomes não
    relacionados (ver comentário acima de BANCOS_ALVO)."""
    return "|".join(rf"\b{re.escape(t)}\b" for t in termos)


def identificar_tier(nome_instituicao: str) -> str:
    nome_upper = str(nome_instituicao).upper()
    for banco in BANCOS_ALVO.values():
        if re.search(_padrao_termos(banco["termos"]), nome_upper):
            return banco["tier"]
    return "outro"


RELATORIO_CARTAO_PF = "11"


IFDATA_BASE_URL = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"


def _ifdata_get(endpoint: str, params: dict = None, top: int = None) -> list[dict]:
    """Chama um endpoint do IFDATA tratando paginação dinâmica através do link de controle OData."""
    if params:
        assinatura = ",".join(f"{k}=@{k}" for k in params)
        query_valores = "&".join(f"@{k}={quote(str(v), safe=chr(39))}" for k, v in params.items())
        url = f"{IFDATA_BASE_URL}/{endpoint}({assinatura})?{query_valores}"
    else:
        url = f"{IFDATA_BASE_URL}/{endpoint}?"

    extras = []
    if top:
        extras.append(f"$top={top}")
    extras.append("$format=json")
    separador = "" if url.endswith("?") else "&"
    url_completa = url + separador + "&".join(extras)

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    resultados = []
    url_atual = url_completa
    
    # Loop de Paginação OData (Olinda BCB)
    while url_atual:
        resp = requests.get(url_atual, headers=headers, timeout=60)
        
        corpo = resp.text.strip()
        if corpo.startswith("/*") and corpo.endswith("*/"):
            corpo = corpo[2:-2].strip()

        if resp.status_code != 200:
            raise RuntimeError(f"IFDATA {endpoint} devolveu {resp.status_code} pra "
                                f"GET {url_atual} - corpo: {corpo[:400]}")

        dados = json.loads(corpo)
        if isinstance(dados, dict):
            resultados.extend(dados.get("value", []))
            # Captura o link da próxima página gerado pelo servidor do BCB
            url_atual = dados.get("@odata.nextLink")
        else:
            if isinstance(dados, list):
                resultados.extend(dados)
            break
            
    return resultados


def normalizar_codigo(val) -> str:
    """
    Garante que códigos de instituições sejam convertidos para string de 8 dígitos.
    Remove letras (C, F, etc.) e sufixos decimais do pandas.
    """
    if pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    s_numerico = "".join(filter(str.isdigit, s))
    if s_numerico:
        return s_numerico.zfill(8)
    return ""


def get_quarters(year: int, defasagem_dias: int = 75) -> list[int]:
    hoje = date.today()
    candidatos = [(year, m) for m in (3, 6, 9, 12)]
    publicados = []
    for ano, mes_fim in candidatos:
        fechamento = date(ano, mes_fim, 28)
        dias_desde_fechamento = (hoje - fechamento).days
        if dias_desde_fechamento >= defasagem_dias:
            publicados.append(ano * 100 + mes_fim)
    return publicados


def get_ifdata_cartao(anomes_list: list[int]) -> pd.DataFrame:
    """Busca a linha 'Cartão de Crédito' do relatório 11 (PF) do IF.data
    pros bancos-alvo, nos trimestres informados."""
    resultados = []
    for anomes in anomes_list:
        # Lógica de migração regulatória (Até 12/2024 = Tipo 2, Pós 03/2025 = Tipo 1)
        tipo_inst = 1 if anomes >= 202503 else 2
        print(f"\n[info] Processando período {anomes} utilizando TipoInstituicao={tipo_inst}")

        # Cadastro limpo apenas com AnoMes
        registros_cadastro = _ifdata_get("IfDataCadastro", params={"AnoMes": anomes})
        cadastro = pd.DataFrame(registros_cadastro)
        if cadastro.empty:
            print(f"[aviso] cadastro veio vazio pra {anomes}")
            continue

        padrao = _padrao_termos(_todos_termos())
        alvo = cadastro[cadastro["NomeInstituicao"].str.contains(padrao, case=False, na=False)].copy()
        if alvo.empty:
            print(f"[aviso] nenhuma instituição-alvo encontrada no cadastro de {anomes}")
            continue
        alvo["tier"] = alvo["NomeInstituicao"].apply(identificar_tier)

        # Mapeamento robusto de códigos candidatos
        campos_codigo_candidatos = ["CodInst", "CodConglomeradoPrudencial", "CodConglomeradoFinanceiro", "CnpjInstituicaoLider"]
        mapa_codigo = {}
        for _, linha in alvo.iterrows():
            info = (linha["NomeInstituicao"], linha["tier"])
            for campo in campos_codigo_candidatos:
                if campo in alvo.columns and pd.notna(linha.get(campo)):
                    cod_limpo = normalizar_codigo(linha[campo])
                    if cod_limpo:
                        mapa_codigo[cod_limpo] = info
        codigos_alvo = list(mapa_codigo.keys())

        # Chamamos os valores contornando a limitação de paginação do Banco Central
        registros_valores = _ifdata_get("IfDataValores", params={
            "AnoMes": anomes,
            "TipoInstituicao": tipo_inst,
            "Relatorio": f"'{RELATORIO_CARTAO_PF}'",
        })
        dados = pd.DataFrame(registros_valores)
        if dados.empty:
            print(f"[aviso] relatório {RELATORIO_CARTAO_PF} vazio para {anomes} (TipoInstituicao={tipo_inst})")
            continue

        # Normaliza CodInst do relatório antes de filtrar
        dados["CodInst_limpo"] = dados["CodInst"].apply(normalizar_codigo)
        
        # Filtramos explicitamente usando um set()
        dados_filtrados = dados[dados["CodInst_limpo"].isin(set(mapa_codigo.keys()))].copy()
        
        if dados_filtrados.empty:
            codinst_no_relatorio = set(dados["CodInst_limpo"].unique())
            amostra_bancos = alvo.head(3).to_dict("records")
            amostra_relatorio = sorted(codinst_no_relatorio)[:5]
            print(f"[aviso] {anomes}: {len(codigos_alvo)} códigos tentados, mas nenhum bateu "
                  f"com as {len(codinst_no_relatorio)} do relatório. Amostra relatório: {amostra_relatorio}")
            continue
            
        dados_filtrados["NomeInstituicao"] = dados_filtrados["CodInst_limpo"].map(lambda c: mapa_codigo[c][0])
        dados_filtrados["tier"] = dados_filtrados["CodInst_limpo"].map(lambda c: mapa_codigo[c][1])
        dados_filtrados["AnoMes"] = anomes
        resultados.append(dados_filtrados)

    if not resultados:
        return pd.DataFrame()

    df = pd.concat(resultados, ignore_index=True)

    mask_cartao = df["NomeColuna"].str.contains("Cartão", case=False, na=False)
    if not mask_cartao.any():
        mask_cartao = df["NomeColuna"].str.contains("Cartao", case=False, na=False)
        
    if not mask_cartao.any():
        colunas_disponiveis = sorted(df["NomeColuna"].dropna().unique().tolist())
        print(f"[aviso] nenhuma coluna com 'Cartão'/'Cartao' encontrada no relatório. Colunas: {colunas_disponiveis}")
        return pd.DataFrame()
        
    return df[mask_cartao]


def listar_instituicoes_alvo(anomes: int) -> pd.DataFrame:
    """Função de apoio: lista o que o cadastro do Bacen tem para os termos
    de busca, para você confirmar o nome oficial antes de automatizar."""
    registros = _ifdata_get("IfDataCadastro", params={"AnoMes": anomes})
    cadastro = pd.DataFrame(registros)
    if cadastro.empty:
        return cadastro

    padrao = _padrao_termos(_todos_termos())
    resultado = cadastro[cadastro["NomeInstituicao"].str.contains(padrao, case=False, na=False)][
        ["CodInst", "NomeInstituicao", "Td", "CodConglomeradoPrudencial"]
    ].copy()
    resultado["tier"] = resultado["NomeInstituicao"].apply(identificar_tier)
    return resultado


# ---------------------------------------------------------------------------
# 5. Ranking de Reclamações - por instituição, trimestral
# ---------------------------------------------------------------------------
#
# Cartão de crédito costuma ser a categoria #1 de reclamação nesse ranking,
# então mesmo não sendo um dado "de cartão" in si, é um proxy forte de
# qualidade operacional em cartão, comparável entre Porto/Itaú/Nubank.

RANKING_RECLAMACOES_URL = "https://www3.bcb.gov.br/rdrweb/rest/ext/ranking/arquivo"


def baixar_ranking_reclamacoes(ano: int, periodo: int, periodicidade: str = "TRIMESTRAL",
                                 tipo: str = "Bancos e financeiras") -> pd.DataFrame:
    """Baixa o CSV do ranking de reclamações de um trimestre específico.

    periodo: 1 a 4 (trimestre) quando periodicidade='TRIMESTRAL'.

    Atenção: as colunas são detectadas por palavra-chave ao invés de nome
    fixo, já que o layout exato pode variar entre trimestres.
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
    """Busca o ranking de reclamações para os bancos-alvo nos
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

        padrao = _padrao_termos(_todos_termos())
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


if __name__ == "__main__":
    print("Baixando séries SGS (nacional, mensal)...")
    sgs_df = get_sgs_cartao(start="2024-01-01")
    sgs_path = os.path.join(OUTPUT_DIR, "sgs_cartao_mensal.csv")
    sgs_df.to_csv(sgs_path)
    print(f"  -> salvo em {sgs_path} ({len(sgs_df)} linhas)")

    # Puxa os trimestres disponíveis dinamicamente
    ano_atual = date.today().year
    quarters_para_buscar = get_quarters(ano_atual) or get_quarters(ano_atual - 1)[-1:]
    
    print(f"\nBaixando IF.data trimestral para {quarters_para_buscar}...")
    ifdata_df = get_ifdata_cartao(quarters_para_buscar)
    if not ifdata_df.empty:
        ifdata_path = os.path.join(OUTPUT_DIR, "ifdata_cartao_trimestral.csv")
        ifdata_df.to_csv(ifdata_path, index=False)
        print(f"  -> salvo em {ifdata_path} ({len(ifdata_df)} linhas)")
    else:
        print("  -> nenhum dado retornado, revise as configurações.")
