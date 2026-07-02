# Painel Cartão · Bacen

Protótipo rápido: cards de "big number + série temporal" pras séries de
cartão de crédito do Bacen (SGS, IF.data por banco, e Ranking de
Reclamações por banco).

## Estrutura

```
index.html              -> o painel (GitHub Pages serve essa pasta)
scripts/bacen_cartao_pipeline.py   -> puxa os dados do Bacen
scripts/generate_data_json.py      -> gera docs/data.json pro painel ler
.github/workflows/atualizar-painel.yml  -> roda tudo automaticamente 1x/semana
requirements.txt             -> python-bcb==0.3.3 pinado
```

## Deploy em 3 passos

1. Cria um repo novo no GitHub e sobe essa pasta inteira (`git init`, `git add .`, `git commit`, `git push`).
2. No repo, vai em **Settings → Pages** e seleciona:
   - Source: **Deploy from a branch**
   - Branch: `gh-pages` / `root` *(essa branch é criada sozinha na primeira vez que o workflow rodar)*
3. Vai em **Actions** e clica **Run workflow** manualmente na primeira vez (não precisa esperar a segunda-feira).

Depois disso o link fica em `https://<seu-usuario>.github.io/<nome-do-repo>/`.

## Rodando local antes de subir

```bash
pip install -r requirements.txt --break-system-packages
python scripts/generate_data_json.py
```

Isso gera `docs/data.json`. Abre `docs/index.html` direto no navegador
(duplo clique) pra conferir antes de subir pro GitHub - **sem precisar de
servidor local**, porque se o `fetch('./data.json')` falhar (ex.: abrindo
o arquivo via `file://`), o painel cai automaticamente nos dados de exemplo
(MOCK) só pra você ver o layout funcionando.

## Ajustes que você provavelmente vai precisar fazer

- `generate_data_json.py`: o nome exato da coluna de valor do relatório 11
  do IF.data (`col_valor`) - roda uma vez e confere `df.columns` antes de
  confiar no automático.
- `BANCOS_ALVO` em `bacen_cartao_pipeline.py`: confirma se "PORTO" realmente
  bate com o nome oficial do PortoBank no cadastro do Bacen.
- Trimestres em `quarters = [202503]` dentro de `generate_data_json.py`:
  vai adicionando 202506, 202509... conforme forem sendo publicados.
