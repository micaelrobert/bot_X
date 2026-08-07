# Telegram → X Mirror

Serviço independente em Python para monitorar **um canal ou grupo específico do Telegram** e republicar automaticamente novas mensagens no X.

O projeto não depende de nenhum sistema externo de promoções. Ele executa como um processo contínuo, mantém sessão do Telegram e do navegador, usa uma fila assíncrona com outbox persistente, baixa mídias sob demanda, evita republicações conhecidas e recupera trabalhos pendentes após reinícios.

## Principais características

- Python 3.12+
- Telethon assíncrono
- Playwright assíncrono com Chromium persistente
- `asyncio.Queue` com outbox em `storage/pending.json`
- Deduplicação em `storage/published.json`
- Suporte a texto, foto, álbum de fotos e vídeo
- Limpeza automática de arquivos temporários
- Retentativas com backoff exponencial e jitter
- Logs no console e em arquivo rotativo diário
- Sessões persistentes do Telegram e do X
- Arquitetura desacoplada para troca futura do Playwright pela API oficial do X
- Encerramento seguro com `SIGINT`/`SIGTERM`
- Supervisão: falhas críticas reiniciam o ciclo do serviço

---

## Arquitetura

```text
Telegram / Telethon
        │
        │ eventos NewMessage e Album
        ▼
TelegramListener
        │
        │ persiste antes de enfileirar
        ▼
storage/pending.json  +  asyncio.Queue
        │
        ▼
Publisher Worker
        │
        ├── recupera mensagens por ID
        ├── baixa mídia temporária
        ├── publica pelo XPublisher
        └── confirma resultado
        ▼
storage/published.json
```

A fila em memória é rápida, mas sozinha perderia mensagens em um encerramento abrupto. Por isso, o projeto adota o padrão **durable outbox**:

1. O evento novo é salvo em `pending.json`.
2. Só depois ele entra na `asyncio.Queue`.
3. Após confirmação da publicação, seus IDs são registrados em `published.json`.
4. O item pendente é removido.
5. Em um reinício, todo item ainda pendente volta automaticamente à fila.

A mídia não precisa estar baixada no momento da captura. O outbox armazena os IDs do Telegram, permitindo que o worker recupere e baixe a mídia novamente após falhas ou reinícios.

---

## Estrutura do projeto

```text
telegram_to_x/
├── app.py                         # Supervisor, ciclo do serviço e worker
├── config.py                      # Configuração e validação do .env
├── logger.py                      # Logging rotativo
├── media.py                       # Download, seleção e limpeza de mídia
├── models.py                      # Modelos de domínio
├── queue_manager.py               # asyncio.Queue + outbox + retries
├── storage.py                     # Persistência JSON atômica
├── telegram_listener.py           # Eventos Telethon
├── text_utils.py                  # Normalização e limite ponderado do X
├── utils.py                       # Utilitários pequenos
├── x_publisher.py                 # Interface e implementação Playwright
│
├── scripts/
│   └── x_login.py                 # Primeiro login manual no X
│
├── tests/
│   ├── test_models.py
│   └── test_text_utils.py
│
├── deploy/
│   └── telegram-to-x.service.example
│
├── storage/
│   ├── published.json             # IDs já processados
│   ├── pending.json               # Outbox persistente
│   ├── telegram.session           # Criado pelo Telethon
│   ├── x_profile/                 # Perfil persistente do Chromium
│   ├── media/                     # Mídia temporária
│   └── logs/                      # Logs rotativos
│
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
├── LICENSE
└── README.md
```

### Por que não existe `session.json`?

O Telethon usa nativamente um arquivo SQLite com extensão `.session`. Manter esse formato evita uma camada customizada desnecessária e preserva toda a lógica de autenticação e atualização de sessão da biblioteca.

Para o X, o Playwright usa um **perfil persistente de Chromium** em `storage/x_profile/`. Esse perfil guarda cookies, local storage e outros dados necessários. Ele é mais robusto do que exportar somente cookies para um JSON.

---

## Pré-requisitos

- Python 3.12 ou superior
- Conta do Telegram com acesso ao canal/grupo monitorado
- `API_ID` e `API_HASH` obtidos em `my.telegram.org`
- Conta do X sob seu controle
- Chromium instalado pelo Playwright
- Em Linux servidor: bibliotecas do Chromium instaladas pelo comando do Playwright

Use este projeto respeitando as regras do Telegram e do X. A automação de interface web é inerentemente mais frágil do que uma API oficial e pode exigir atualização de seletores quando o X alterar sua interface.

---

## Instalação — Linux

```bash
cd telegram_to_x
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install --with-deps chromium
```

Em distribuições nas quais `python3.12` não estiver no PATH, use o caminho da instalação correspondente.

## Instalação — Windows PowerShell

```powershell
cd telegram_to_x
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

Caso a política do PowerShell bloqueie a ativação do ambiente:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## Configuração

Copie o exemplo quando necessário:

```bash
cp .env.example .env
```

No Windows:

```powershell
Copy-Item .env.example .env
```

Exemplo mínimo:

```dotenv
API_ID=12345678
API_HASH=seu_api_hash
CHANNEL=@canal_monitorado
TELEGRAM_PHONE=+5511999999999

TWITTER_USERNAME=@seu_usuario
TWITTER_PASSWORD=
TWITTER_EMAIL=

X_AUTO_LOGIN=false
X_HEADLESS=true
```

### Variáveis do Telegram

| Variável | Obrigatória | Descrição |
|---|---:|---|
| `API_ID` | Sim | ID numérico da aplicação Telegram |
| `API_HASH` | Sim | Hash da aplicação Telegram |
| `CHANNEL` | Sim | `@username`, link resolvível ou ID do canal/grupo |
| `TELEGRAM_PHONE` | No primeiro login | Telefone em formato internacional |
| `TELEGRAM_2FA_PASSWORD` | Somente quando aplicável | Senha de verificação em duas etapas |

Depois de gerar `storage/telegram.session`, o telefone e a senha 2FA normalmente podem ser removidos do `.env`.

### Variáveis do X

| Variável | Padrão | Descrição |
|---|---:|---|
| `TWITTER_USERNAME` | obrigatório | Usuário do perfil; necessário para URL e reconciliação |
| `TWITTER_PASSWORD` | vazio | Usado somente quando `X_AUTO_LOGIN=true` |
| `TWITTER_EMAIL` | vazio | Pode ser solicitado pelo fluxo de desafio do login |
| `X_AUTO_LOGIN` | `false` | Tenta login por credenciais; login manual é recomendado |
| `X_HEADLESS` | `true` | Executa Chromium sem janela |
| `X_BASE_URL` | `https://x.com` | URL base do X |
| `X_CHAR_LIMIT` | `280` | Limite ponderado aplicado ao texto |
| `X_MAX_IMAGES` | `4` | Máximo de imagens enviadas em um post |
| `X_NAVIGATION_TIMEOUT_MS` | `45000` | Timeout de navegação e seletores |
| `X_UPLOAD_TIMEOUT_MS` | `180000` | Timeout de upload e publicação |

### Variáveis de resiliência

| Variável | Padrão | Descrição |
|---|---:|---|
| `RETRY_BASE_SECONDS` | `5` | Base do backoff exponencial |
| `RETRY_MAX_SECONDS` | `300` | Limite máximo entre tentativas |
| `STARTUP_GRACE_SECONDS` | `0` | Tolerância de data ao iniciar; zero ignora tudo que antecede o início |
| `LOG_LEVEL` | `INFO` | Nível de log |
| `STORAGE_DIR` | `storage` | Diretório de persistência |

Nada sensível fica hardcoded. O `.env`, a sessão do Telegram e o perfil do Chromium não devem ser enviados a repositórios ou compartilhados.

---

## Primeiro login do Telegram

Execute o serviço manualmente em um terminal interativo:

```bash
python app.py
```

No primeiro acesso, o Telethon pode solicitar:

1. Número de telefone
2. Código recebido no Telegram
3. Senha 2FA, quando habilitada

Após a autenticação, será criado:

```text
storage/telegram.session
```

Interrompa com `Ctrl+C` depois de confirmar no log que o Telegram conectou. Esse procedimento deve ser concluído antes de instalar o serviço em `systemd`.

A conta autenticada precisa conseguir visualizar o canal/grupo configurado. Em canais privados, ela precisa ser membro.

---

## Primeiro login do X

O método recomendado é o login manual em um perfil separado do Chromium:

1. Temporariamente, mantenha o serviço principal parado.
2. Execute:

```bash
python scripts/x_login.py
```

3. Faça login na janela aberta.
4. Conclua CAPTCHA, 2FA ou desafio de segurança.
5. Confirme que a página inicial do X abriu.
6. Volte ao terminal e pressione `ENTER`.

O perfil será salvo em:

```text
storage/x_profile/
```

Depois disso, use:

```dotenv
X_AUTO_LOGIN=false
X_HEADLESS=true
```

O Chromium normal do usuário não deve apontar para esse mesmo diretório enquanto o bot estiver executando. Um perfil persistente só pode ser aberto por uma instância de navegador por vez.

### Login automático opcional

Para ambientes sem possibilidade de login manual:

```dotenv
X_AUTO_LOGIN=true
TWITTER_USERNAME=@usuario
TWITTER_PASSWORD=senha
TWITTER_EMAIL=email_de_confirmacao
```

Esse modo é apenas um fallback. CAPTCHA, passkey, 2FA e mudanças no fluxo do X podem impedir a autenticação. Prefira criar a sessão manualmente e remover a senha do `.env`.

---

## Execução

```bash
python app.py
```

O processo permanece ativo até receber `Ctrl+C`, `SIGINT` ou `SIGTERM`.

Fluxo esperado no log:

```text
2026-07-06 08:15:00 | INFO | telegram_listener | Nova mensagem recebida | telegram_id=1234
2026-07-06 08:15:00 | INFO | queue_manager | Mensagem adicionada à fila
2026-07-06 08:15:01 | INFO | media | Mídia baixada
2026-07-06 08:15:03 | INFO | x_publisher | Publicando no X...
2026-07-06 08:15:05 | INFO | x_publisher | Publicado com sucesso | telegram_ids=[1234] | x_id=...
```

Logs completos e exceções com traceback ficam em:

```text
storage/logs/telegram_to_x.log
```

Os arquivos giram diariamente e são mantidos por 30 dias.

---

## Regras para mensagens novas

O listener registra o instante em que o serviço conectou e ignora mensagens cuja data seja anterior a esse instante.

O projeto **não percorre o histórico** do canal durante o startup. Isso impede que mensagens antigas sejam publicadas na primeira execução.

`STARTUP_GRACE_SECONDS=0` aplica a regra mais estrita. Um valor positivo pode ser usado apenas se houver diferença de relógio entre o servidor e o Telegram.

Mensagens já capturadas antes de um reinício continuam sendo processadas porque estão no outbox, mesmo que a data delas seja anterior ao novo startup.

---

## Tratamento de texto

`text_utils.py`:

- normaliza Unicode em NFC;
- remove espaços horizontais duplicados;
- reduz excesso de linhas vazias;
- preserva emojis;
- preserva parágrafos importantes;
- extrai links ocultos de entidades `MessageEntityTextUrl` e os acrescenta ao conteúdo;
- calcula comprimento ponderado compatível com a configuração `twitter-text` v2;
- conta cada URL como o tamanho transformado do `t.co`;
- trunca em limite natural de palavra;
- usa reticências;
- nunca divide um link;
- quando há truncamento, prioriza manter links inteiros no final.

Quando a quantidade total de links, sozinha, ultrapassa o limite do X, somente os links inteiros que couberem são mantidos. Nenhum link parcial é publicado.

---

## Regras de mídia

### Foto única

A foto é baixada para a pasta temporária do job e anexada ao post.

### Álbum

O evento `Album` do Telethon agrupa as mensagens. O bot publica até `X_MAX_IMAGES`, limitado a quatro pelo validador de configuração.

### Vídeo

O bot seleciona um vídeo e publica junto com o texto.

### Álbum misto

Quando há vídeo e fotos no mesmo grupo, o vídeo tem prioridade. Fotos e vídeos excedentes são ignorados e registrados no log. Isso evita combinações que o compositor do X normalmente não aceita como um único post.

### Arquivos não suportados

Documentos, áudios e outros anexos são ignorados. Se a mensagem não possuir texto nem mídia suportada, ela é marcada como processada para não entrar em loop infinito.

### Limpeza

A pasta de mídia de cada job é removida após sucesso ou falha. Em uma retentativa, a mídia é baixada novamente a partir dos IDs persistidos do Telegram.

---

## Duplicidade e consistência

A chave de deduplicação é:

```text
<chat_id>:<message_id>
```

Para álbuns, todos os IDs são gravados com o mesmo resultado do X.

Antes de enfileirar e antes de publicar, o projeto consulta `published.json`. Uma mensagem conhecida não é republicada.

### Janela não idempotente do navegador

Publicar pela interface web possui uma limitação distribuída inevitável: o X não oferece ao Playwright uma chave de idempotência controlada pelo projeto. Existe uma pequena janela entre:

1. o X aceitar o clique de publicação; e
2. o processo gravar a confirmação local.

O projeto reduz esse risco com um estado `publishing` persistido **antes do clique**. Após uma resposta ambígua ou reinício nessa janela, ele procura no perfil um post recente com o mesmo texto e horário aproximado. Quando encontra, registra o ID sem clicar novamente.

Para posts com texto, isso fornece uma reconciliação prática muito forte. Para mensagens exclusivamente de mídia, uma garantia matemática de exactly-once não é possível usando somente automação da interface. Se essa garantia for requisito contratual absoluto, substitua o adaptador Playwright pela API oficial do X e use o mecanismo de idempotência/consulta oferecido pela integração escolhida.

Fora dessa janela extrema, `published.json` e `pending.json` impedem duplicidade em reinícios e retentativas normais.

---

## Retentativas

Falhas de rede, Telegram, download, navegador ou publicação não removem o job.

O atraso usa backoff exponencial com jitter:

```text
5s → 10s → 20s → 40s → ... → máximo configurado
```

Erros posteriores ao clique são classificados como ambíguos. Na próxima tentativa, o worker executa reconciliação antes de considerar uma nova publicação.

O supervisor reinicia o ciclo completo caso o Telethon desconecte inesperadamente ou ocorra uma falha crítica fora do worker.

---

## Execução contínua com systemd

1. Copie o projeto para um diretório, por exemplo:

```text
/opt/telegram_to_x
```

2. Ajuste usuário, grupo e caminhos em:

```text
deploy/telegram-to-x.service.example
```

3. Instale:

```bash
sudo cp deploy/telegram-to-x.service.example /etc/systemd/system/telegram-to-x.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-to-x
```

4. Consulte o estado:

```bash
sudo systemctl status telegram-to-x
```

5. Acompanhe logs do serviço:

```bash
sudo journalctl -u telegram-to-x -f
```

6. Reinicie após atualizar o código:

```bash
sudo systemctl restart telegram-to-x
```

O `Restart=always` do systemd complementa o supervisor interno. O arquivo de exemplo concede escrita apenas ao diretório `storage` dentro do projeto.

---

## Execução contínua no Windows

Use o Agendador de Tarefas:

1. Crie uma tarefa executada ao iniciar o sistema ou ao fazer logon.
2. Programa:

```text
C:\caminho\telegram_to_x\.venv\Scripts\python.exe
```

3. Argumentos:

```text
C:\caminho\telegram_to_x\app.py
```

4. Diretório inicial:

```text
C:\caminho\telegram_to_x
```

5. Ative a opção de reiniciar a tarefa em caso de falha.
6. Use uma conta com permissão de leitura e escrita no diretório `storage`.

---

## Testes

A suíte usa `unittest`, sem dependência adicional:

```bash
python -m unittest discover -s tests -v
```

Os testes cobrem serialização do job, normalização, comprimento ponderado, truncamento e preservação de URL.

---

## Atualização

Antes de atualizar:

1. Pare o serviço.
2. Faça backup de:

```text
.env
storage/telegram.session
storage/x_profile/
storage/published.json
storage/pending.json
```

3. Atualize o código.
4. Ative o ambiente virtual.
5. Reinstale dependências:

```bash
pip install --upgrade -r requirements.txt
playwright install chromium
```

6. Execute os testes.
7. Inicie o serviço.

Nunca apague `pending.json` durante uma atualização com mensagens pendentes. Nunca substitua `published.json` por um arquivo vazio em produção, pois isso remove a memória de deduplicação.

---

## Troca futura para a API oficial do X

`x_publisher.py` define a abstração `XPublisher` com os métodos:

```python
start()
publish(job, media_paths)
reconcile(job)
reset()
stop()
```

Para migrar:

1. Crie `OfficialApiXPublisher(XPublisher)`.
2. Implemente upload de mídia e criação de post.
3. Retorne `PublishResult` com ID e URL.
4. Troque apenas a instanciação no `app.py`.

Listener, fila, persistência, mídia, logs e deduplicação permanecem inalterados.

---

## Solução de problemas

### `Erro de configuração: Variável obrigatória ausente`

Preencha `API_ID`, `API_HASH` e `CHANNEL` no `.env`. Confirme que o arquivo está na raiz do projeto.

### O Telegram pede login toda vez

Verifique:

- permissão de escrita em `storage`;
- existência de `storage/telegram.session`;
- usuário do systemd;
- se o diretório não está sendo recriado a cada deploy.

### O canal não é encontrado

- Confirme `@username` ou ID.
- Confirme que a conta autenticada é membro do canal privado.
- Abra o canal uma vez no aplicativo oficial do Telegram.

### Mensagens antigas foram detectadas

Mantenha:

```dotenv
STARTUP_GRACE_SECONDS=0
```

Confirme também que o relógio do servidor está sincronizado por NTP.

### `Sessão do X ausente`

Pare o bot e execute:

```bash
python scripts/x_login.py
```

### O Chromium fecha imediatamente

- Rode `playwright install --with-deps chromium` no Linux.
- Confirme permissões em `storage/x_profile`.
- Não abra duas instâncias com o mesmo perfil.
- Em container, mantenha espaço suficiente em `/dev/shm`; o projeto já usa `--disable-dev-shm-usage`.

### O X solicita CAPTCHA ou 2FA

Use o login manual persistente. O bot não tenta contornar CAPTCHA, 2FA ou mecanismos de segurança.

### `Botão Publicar não encontrado`

A interface do X pode ter mudado. Revise em `x_publisher.py`:

```text
[data-testid="tweetTextarea_0"]
[data-testid="fileInput"]
[data-testid="tweetButton"]
[data-testid="tweetButtonInline"]
```

Faça essa validação em ambiente de teste antes de atualizar produção.

### Upload de vídeo demora ou falha

- Aumente `X_UPLOAD_TIMEOUT_MS`.
- Verifique tamanho, duração e codec aceitos pela conta no X.
- Confirme banda e espaço em disco.
- Consulte o traceback completo no log.

### Um job fica repetindo

Abra `storage/pending.json` e consulte `last_error` e `attempts`. Não remova o job antes de entender a causa.

Para descartar conscientemente um job:

1. Pare o serviço.
2. Faça backup de `pending.json`.
3. Remova somente a chave correspondente em `jobs`.
4. Reinicie.

### JSON corrompido após queda de energia

As gravações usam arquivo temporário, `fsync` e `os.replace`, reduzindo o risco de corrupção. Mantenha backups regulares de `published.json` e `pending.json`.

---

## Segurança operacional

- Use permissões restritas no `.env`:

```bash
chmod 600 .env
```

- Restrinja a sessão do Telegram e o perfil do X ao usuário do serviço.
- Não envie logs contendo conteúdo privado a terceiros.
- Não execute o bot como `root` sem necessidade.
- Use um usuário de sistema dedicado em produção.
- Faça backup criptografado das sessões quando necessário.
- Remova `TWITTER_PASSWORD` e `TELEGRAM_2FA_PASSWORD` após criar as sessões, sempre que possível.
- Não compartilhe `API_HASH`, `.session` ou `x_profile`.

---

## Observações de manutenção

- O formato de `published.json` e `pending.json` possui campo `version` para permitir migrações futuras.
- O worker é sequencial por design, evitando duas publicações simultâneas no mesmo compositor.
- A fila aceita várias mensagens rapidamente; elas aguardam ordenadamente.
- A implementação usa um lock no publisher, portanto continua segura caso outros produtores sejam adicionados.
- O listener ignora `NewMessage` com `grouped_id`, pois o mesmo conteúdo é tratado pelo evento `Album`.
- Links de texto ocultos do Telegram são acrescentados ao final para não serem perdidos.
- O projeto não publica edições posteriores de mensagens. Somente eventos novos são monitorados.

---

## Licença

MIT. Consulte `LICENSE`.
