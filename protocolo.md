# Protocolo de Transferência de Arquivos via BLE  
Servidor: `bt_ble_file.py` (Python-BLE)

Este documento descreve:

- O **protocolo de comunicação** entre o app (central BLE) e o Raspberry Pi (periférico BLE).
- Como o script **`bt_ble_file.py`** implementa esse protocolo em Python.
- O papel das principais **funções e classes** no fluxo de transferência de arquivos.

---

## 1. Visão Geral

O Raspberry Pi atua como **periférico BLE** com um serviço estilo **Nordic UART**:

- **Service UUID (NUS-like)**: `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
- **RX Characteristic (Write)**: `6e400002-b5a3-f393-e0a9-e50e24dcca9e`  
  Usada pelo app para enviar JSON de controle + bytes do arquivo.
- **TX Characteristic (Notify)**: `6e400003-b5a3-f393-e0a9-e50e24dcca9e`  
  Atualmente o servidor só atualiza o valor internamente; pode ser usado no futuro para ACKs.

O app (central):

1. Conecta no Pi.
2. Envia um JSON `"file_begin"` para iniciar a transferência.
3. Envia blocos binários (chunks) com os bytes do arquivo.
4. Envia um JSON `"file_end"` para finalizar.
5. O Pi grava o arquivo em `./downloads/<nome>`.

---

## 2. Estrutura do Serviço BLE

Constantes principais no `bt_ble_file.py`:

```python
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write, WriteWithoutResponse
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notify (Read opcional)

DOWNLOAD_DIR = os.path.abspath("./downloads")
```

- `NUS_SERVICE_UUID`: identifica o serviço tipo UART.
- `NUS_RX_CHAR_UUID`: característica que recebe todos os dados do app:
  - JSON de controle
  - Dados binários do arquivo
- `NUS_TX_CHAR_UUID`: característica de notificação (pode ser usada futuramente para ACKs).

---

## 3. Formato das Mensagens

A comunicação é dividida em **dois tipos**:

1. **Mensagens de Controle (JSON)** – delimitam o arquivo.
2. **Blocos Binários** – conteúdo do arquivo em si.

### 3.1. Mensagem `file_begin`

JSON enviado **antes** do envio dos bytes:

```json
{
  "op": "file_begin",
  "name": "foto_teste.jpg",
  "size": 113436
}
```

Campos:

- `op`: sempre `"file_begin"`.
- `name`: nome do arquivo que será salvo.
- `size`: tamanho esperado do arquivo em bytes (informativo – ainda não é validado strict).

### 3.2. Mensagem `file_end`

JSON enviado **depois** dos bytes:

```json
{
  "op": "file_end"
}
```

Campos:

- `op`: sempre `"file_end"`.

### 3.3. Blocos Binários do Arquivo

Entre `file_begin` e `file_end`, o app envia blocos binários (chunks) com os bytes do arquivo:

- Enviados como `Uint8Array` / `byte[]` na RX.
- Não existe cabeçalho no nível do protocolo Python:
  - O servidor simplesmente concatena tudo em `bytearray`.

---

## 4. Máquina de Estados do Servidor

A lógica de controle está na classe **`BLEApp`**, com os seguintes atributos:

- `recv_enabled: bool`  
  Liga/desliga o modo de recebimento (`e` no teclado).
- `recv_active: bool`  
  Indica se há uma transferência de arquivo em andamento.
- `recv_name: str | None`  
  Nome do arquivo atual.
- `recv_size: int`  
  Tamanho esperado (informado pelo app).
- `recv_bytes: bytearray | None`  
  Buffer com os bytes recebidos.
- `recv_chunks: int`  
  Quantidade de blocos binários recebidos.
- `recv_t0: float`  
  Timestamp de início do arquivo (para estatísticas).
- `recv_last_saved_path: str | None`  
  Caminho do último arquivo salvo.

### 4.1. Estados

- **Idle (Parado)**  
  `recv_active = False`, sem arquivo em andamento.

- **Armado para recebimento**  
  `recv_enabled = True`, mas ainda não recebeu `file_begin`.

- **Recebendo**  
  Após `file_begin`, o servidor:
  - Zera buffer
  - Seta `recv_active = True`
  - Acumula bytes nos próximos writes

- **Finalização**  
  Ao receber `file_end`:
  - Grava o arquivo em disco
  - Zera `recv_active` e variáveis do arquivo

---

## 5. Fluxo Completo da Transferência

### 5.1. Passo a passo (lado do app)

1. Conectar ao dispositivo BLE (`Pi-BLE-UART`).
2. Descobrir serviços/características.
3. Habilitar Notify na TX (opcional, para futuras respostas).
4. Enviar JSON `file_begin` na RX.
5. Enviar todos os chunks binários na RX.
6. Enviar JSON `file_end` na RX.

### 5.2. Passo a passo (lado do servidor Python)

Tudo passa pelo método **`_on_rx(self, data: bytes)`** da classe `BLEApp`:

```python
def _on_rx(self, data: bytes):
    self.rx_buffer.append(data)

    # Tenta decodificar como JSON
    try:
        text = data.decode("utf-8")
        payload = json.loads(text)
        op = (payload.get("op") or "").lower()
    except Exception:
        op = None
```

1. O dado recebido é armazenado em `rx_buffer` (histórico).
2. O servidor tenta interpretar `data` como JSON:
   - Se der certo e houver campo `op`, segue a lógica de controle;
   - Se falhar, assume que são **dados binários** do arquivo.

#### 5.2.1. Tratando `file_begin`

```python
if op == "file_begin":
    if not self.recv_enabled:
        return
    self.recv_active = True
    self.recv_name = _safe_filename(payload.get("name", "arquivo.bin"))
    self.recv_size = int(payload.get("size", 0))
    self.recv_bytes = bytearray()
    self.recv_chunks = 0
    self.recv_t0 = time.perf_counter()
    print(f"[FILE] BEGIN '{self.recv_name}' size={self.recv_size}")
    return
```

- Se `recv_enabled` estiver desligado, o comando é ignorado.
- Inicializa o buffer (`recv_bytes`) e as estatísticas.
- Usa `_safe_filename` para garantir que o nome não contenha caminhos estranhos.
- Marca o início da transferência (`recv_active = True`).

#### 5.2.2. Tratando `file_end`

```python
elif op == "file_end":
    if not (self.recv_enabled and self.recv_active and self.recv_bytes is not None):
        print("[FILE] file_end ignorado (sem begin).")
        return

    elapsed = (time.perf_counter() - self.recv_t0) * 1000
    path = os.path.join(DOWNLOAD_DIR, self.recv_name or "arquivo.bin")

    if len(self.recv_bytes) == 0:
        print("[FILE] Nada recebido entre begin/end, abortando.")
        self.recv_active = False
        return

    with open(path, "wb") as f:
        f.write(self.recv_bytes)

    print(f"[FILE] END saved={len(self.recv_bytes)} bytes path={path}")
    print(f"[FILE] STATS chunks={self.recv_chunks} elapsed_ms={elapsed:.0f}")
    self.recv_last_saved_path = path
    self.recv_active = False
    self.recv_name = None
    self.recv_bytes = None
    return
```

- Só aceita `file_end` se:
  - o recebimento estiver habilitado (`recv_enabled`),
  - houver uma transferência ativa (`recv_active`),
  - e o buffer não for `None`.
- Calcula tempo decorrido (`elapsed_ms`).
- Se nada foi recebido (`len(recv_bytes) == 0`), aborta.
- Grava o arquivo em `DOWNLOAD_DIR`.
- Mostra estatísticas no log.
- Limpa variáveis do arquivo.

#### 5.2.3. Tratando dados binários

Se não foi possível interpretar o pacote como JSON **e** estamos no meio de uma transferência:

```python
# === 2) Dados binários: arquivo ===
if not (self.recv_active and self.recv_bytes is not None):
    # ignora se não estivermos no meio de um envio
    return

raw = bytes(data)
if not raw:
    return

self.recv_bytes.extend(raw)
self.recv_chunks += 1

if self.recv_chunks % 100 == 0:
    print(f"[FILE] chunk #{self.recv_chunks} · total {len(self.recv_bytes)} bytes")
```

- Ignora se não houver uma transferência ativa.
- Se `data` não estiver vazio:
  - Acrescenta ao buffer `recv_bytes`.
  - Incrementa `recv_chunks`.
  - A cada 100 blocos, imprime um log de progresso.

---

## 6. Referência das Funções e Classes (lado Python)

A seguir, as principais classes e funções do servidor BLE e como elas se relacionam com o protocolo.

### 6.1. `KeyReader`

```python
class KeyReader:
    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

    def restore(self):
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
```

- Coloca o terminal em modo **cbreak** para ler teclas sem precisar apertar Enter.
- Usado para controlar o app com:
  - `a` (start advertising)
  - `s` (stop advertising)
  - `e` (liga/desliga recebimento de arquivo)
  - `v` (mostra último arquivo salvo)
  - `q` (sair)

### 6.2. `NoIOAgent`

```python
class NoIOAgent(ServiceInterface):
    ...
```

- Implementa um Agent BlueZ do tipo **NoInputNoOutput**.
- Permite pareamento **Just Works**, sem PIN.
- Não participa diretamente do protocolo de arquivo, mas é essencial para permitir que o app conecte facilmente ao Pi.

### 6.3. `GattCharacteristic`

```python
class GattCharacteristic(ServiceInterface):
    ...
    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}') -> None:
        incoming = bytes(value)
        self._value = incoming
        ...
        if self.received_cb:
            self.received_cb(incoming)
```

- Representa uma **característica GATT**.
- Para a RX:
  - Quando o app escreve, `WriteValue` é chamado.
  - Esse método converte `value` em `bytes` e chama `self.received_cb(incoming)`.
- A RX é ligada à função `_on_rx` da classe `BLEApp`, que contém toda a lógica do protocolo.

### 6.4. `GattService`

```python
class GattService(ServiceInterface):
    ...
```

- Representa o **serviço GATT** no BlueZ.
- Agrupa as características RX e TX.
- Apenas define UUID e se o serviço é primário.

### 6.5. `LEAdvertisement`

```python
class LEAdvertisement(ServiceInterface):
    ...
    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> 's':
        return "peripheral"
```

- Define o **anúncio BLE**:
  - Nome do dispositivo: `Pi-BLE-UART`.
  - UUID do serviço: `NUS_SERVICE_UUID`.
- Permite que o app encontre o Raspberry Pi na lista de dispositivos.

### 6.6. Função utilitária `_safe_filename`

```python
def _safe_filename(name: str) -> str:
    # só tira barras e null, o resto mantém
    name = os.path.basename(name).strip().replace(" ", "")
    return name or "arquivo.bin"
```

- Protege contra nomes de arquivo malformados:
  - Remove barras (evita caminhos como `../../etc/passwd`),
  - Remove caracteres ` `.
- Garante um fallback seguro (`arquivo.bin`).

### 6.7. Classe `BLEApp`

Responsável por:

- Conectar ao D-Bus.
- Registrar Agent, GATT service e Advertisement.
- Processar dados da RX (_protocolo de arquivo_).
- Controlar o loop de teclas.

#### 6.7.1. `__init__`

```python
class BLEApp:
    def __init__(self):
        ...
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
```

- Inicializa variáveis do protocolo.
- Garante que o diretório `downloads/` exista.

#### 6.7.2. `connect_bus`, `get_managed_objects`, `resolve_adapter`

```python
async def connect_bus(self):
    self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
```

- Conectam ao D-Bus de sistema e localizam o adaptador Bluetooth.
- Necessário para interagir com o BlueZ (serviços, advertising, etc.).

#### 6.7.3. `register_agent`

```python
async def register_agent(self):
    agent = NoIOAgent()
    self.bus.export(AGENT_PATH, agent)
    ...
    await mgr.call_register_agent(AGENT_PATH, "NoInputNoOutput")
    await mgr.call_request_default_agent(AGENT_PATH)
```

- Registra o `NoIOAgent` como agente padrão.
- Permite pareamento Just Works.

#### 6.7.4. `register_gatt`

```python
async def register_gatt(self):
    self.service = GattService(NUS_SERVICE_UUID, True, f"{APP_ROOT}/service0")
    ...
    self.rx_char = GattCharacteristic(NUS_RX_CHAR_UUID, ["write", "write-without-response"], f"{APP_ROOT}/service0")
    self.rx_char.received_cb = self._on_rx
    ...
    await gatt_mgr.call_register_application(APP_ROOT, {})
    print("[GATT] Serviço NUS registrado.")
```

- Cria e registra o serviço NUS.
- Cria RX/TX e exporta para o D-Bus.
- **Ponto importante:** conecta `self.rx_char.received_cb` ao `_on_rx`, que implementa o protocolo.

#### 6.7.5. `_on_rx`

Já detalhado na seção 5.2, é o **coração do protocolo de arquivo**.

#### 6.7.6. `start_advertising` / `stop_advertising`

```python
async def start_advertising(self):
    ...
    self.adv = LEAdvertisement("Pi-BLE-UART", [NUS_SERVICE_UUID])
    ...
    await adv_mgr.call_register_advertisement(ADV_PATH, {})
```

- Controlam quando o dispositivo aparece para o app.
- Podem ser acionados via teclado (tecla `a` e `s`).

#### 6.7.7. `handle_key`

```python
async def handle_key(self, ch):
    ch = ch.strip().lower()
    if ch == "a":
        await self.start_advertising()
    elif ch == "s":
        await self.stop_advertising()
    elif ch == "e":
        self.recv_enabled = not self.recv_enabled
        print(f"[FILE] Receber arquivo: {'ON' if self.recv_enabled else 'OFF'}")
    elif ch == "v":
        print(f"[FILE] Último salvo: {self.recv_last_saved_path or '-'}")
    elif ch == "q":
        print("[SYS] Encerrando…")
        self._shutdown.set()
```

- Lê comandos do usuário no terminal:
  - `e` influencia diretamente o protocolo (`recv_enabled`).
  - `v` ajuda a diagnosticar se o arquivo foi salvo corretamente.

#### 6.7.8. `run`

```python
async def run(self):
    await self.connect_bus()
    await self.resolve_adapter()
    await self.register_agent()
    await self.register_gatt()
    await self.start_advertising()
    print_controls()
    ...
    await self._shutdown.wait()
    ...
```

- Inicializa tudo:
  - D-Bus, adaptador, Agent, GATT, advertising.
- Cria o leitor de teclas e associa ao loop asyncio.
- Fica aguardando `_shutdown` (tecla `q` ou erro).

---

## 7. Logs e Diagnóstico

Exemplos de logs importantes:

- Início do serviço:

  ```text
  [GATT] Serviço NUS registrado.
  [ADV] Advertising iniciado.
  ```

- Início da transferência:

  ```text
  [FILE] BEGIN 'foto_teste.jpg' size=113436
  ```

- Progresso (a cada 100 chunks):

  ```text
  [FILE] chunk #100 · total 24400 bytes
  ```

- Finalização:

  ```text
  [FILE] END saved=113436 bytes path=/caminho/para/downloads/foto_teste.jpg
  [FILE] STATS chunks=465 elapsed_ms=2100
  ```

- Problemas de protocolo:

  ```text
  [FILE] file_end ignorado (sem begin).
  [FILE] Nada recebido entre begin/end, abortando.
  ```

---

## 8. Limitações e Ideias Futuras

- O protocolo não valida rigidamente o `size` informado.
- Não há checksum ou verificação de integridade.
- A TX ainda não é usada para ACK/respostas ao app.

Possíveis extensões:

- Enviar ACKs pela TX:
  - `{"status": "ok", "op": "file_begin"}`  
- Adicionar SHA-256 no `file_end` para verificação.
- Implementar timeout: abortar se não chegar nada depois de X segundos.

---

## 9. Resumo

- O app conversa com o Raspberry Pi usando **mensagens JSON** para controle e **bytes puros** para o arquivo.
- O script `bt_ble_file.py` implementa uma máquina de estados simples (`file_begin` → chunks → `file_end`) no método `_on_rx`.
- A classe `BLEApp` concentra a lógica do protocolo, enquanto as classes GATT e Agent fazem a ponte com o BlueZ/D-Bus.

Este documento serve como referência tanto para quem mexe no **servidor Python**, quanto para quem implementa o **lado cliente** (app mobile ou outro dispositivo BLE).
