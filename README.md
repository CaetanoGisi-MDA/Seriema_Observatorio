# Clarim — Observatório

Agregador de notícias por grupos (RSS, sitemap ou entrada manual), com coleta
diária via GitHub Actions e feed em GitHub Pages, estilo Feedly.

## Como colocar no ar

1. **Criar o repositório**
   Crie um repositório público chamado `Observatorio_Clarim` na sua conta e
   suba todos estes arquivos (`git init`, `git add .`, `git commit`, `git push`).

2. **Criar a chave do Gemini (Google AI Studio)**
   Gere uma API key em https://aistudio.google.com/apikey.
   No repositório: **Settings → Secrets and variables → Actions → New repository secret**
   - Nome: `GEMINI_API_KEY`
   - Valor: a chave gerada

3. **Ativar o GitHub Pages**
   **Settings → Pages → Source: Deploy from a branch → Branch: `main` / `root`**
   O feed fica em `https://<seu-usuário>.github.io/Observatorio_Clarim/`.

4. **Criar o token classic**
   **Settings da sua conta → Developer settings → Personal access tokens → Tokens (classic)**
   Escopo necessário: `repo` (para ler/escrever `config.json` e disparar a Action) e
   `workflow` (para disparar o workflow manualmente).
   Guarde esse token — ele só vai ser colado na tela `admin.html`, dentro do seu navegador.

5. **Editar `config.json`**
   Os três sites do grupo `observatorio` estão como placeholder — edite direto
   pelo GitHub ou pela tela `admin.html` (aba "Grupos e sites") com as URLs reais.

6. **Testar a coleta manualmente**
   Na aba **Actions** do repositório, rode o workflow "Coletar notícias" manualmente
   (`Run workflow`) para conferir se está tudo certo antes de esperar o cron do dia seguinte.

## Estrutura

```
config.json              grupos, sites e status de cada site
data/<grupo>.json        notícias acumuladas daquele grupo (nunca apagadas)
scripts/coletor.py       lógica de coleta (RSS / sitemap / manual) + resumo via Gemini
.github/workflows/       roda todo dia às 03h (BRT) e sob demanda
index.html               feed público (GitHub Pages)
admin.html                configuração de acesso, grupos/sites e adição manual
```

## Modos de site

- **rss** — roda sozinho todo dia.
- **sitemap** — sem RSS, mas o site tem `sitemap.xml`; roda sozinho.
- **manual** — sem RSS nem sitemap; cada notícia é adicionada colando o link em
  `admin.html`. O app extrai título/imagem/resumo automaticamente daquele link,
  mas não descobre sozinho as próximas notícias do mesmo site.

Um site fica marcado com um aviso discreto quando passa **30 dias sem nenhuma
notícia nova** — sinal de que o RSS/sitemap pode ter quebrado e precisa de revisão.

## Fase 2 (ainda não implementada)

Scraping "aprendido": você seleciona visualmente um item de uma listagem sem
RSS/sitemap, o app generaliza o padrão repetido (container → título, link,
imagem) e passa a reconhecer notícias novas daquele site sozinho, sem precisar
colar link a cada notícia — até que a estrutura do site mude.
