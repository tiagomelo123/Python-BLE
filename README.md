# bt_ble_file – Raspberry Pi BLE File Receiver

Script em Python para transformar um **Raspberry Pi 3** em um periférico **BLE** (Bluetooth Low Energy) com:

- Serviço estilo **Nordic UART** (NUS: RX=Write, TX=Notify)
- **Pareamento silencioso (Just Works)** via Agent `NoInputNoOutput`
- **Controle por teclado** no terminal (start/stop advertising, toggle receber arquivo, etc.)
- **Recebimento de arquivos** enviados por BLE:
  - App envia JSON com `op: "file_begin"` / `op: "file_end"`
  - Dados do arquivo enviados como binário puro entre esses comandos
  - Arquivo salvo em `./downloads`

> Testado em Raspberry Pi 3 com BlueZ (D-Bus) e Python 3.

---

## Visão geral

Este script registra um **GATT Server** no BlueZ, expondo um serviço tipo Nordic UART:

- **Service UUID**: `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
- **RX Characteristic (Write)**: `6e400002-b5a3-f393-e0a9-e50e24dcca9e`
- **TX Characteristic (Notify)**: `6e400003-b5a3-f393-e0a9-e50e24dcca9e`

A comunicação é feita assim:

1. Dispositivo central (ex.: app mobile) conecta no Pi.
2. Envia um JSON `file_begin` com nome e tamanho do arquivo.
3. Envia os bytes do arquivo em blocos binários (BLE chunks).
4. Envia um JSON `file_end`.
5. O Pi grava o arquivo em `./downloads/<nome_do_arquivo>`.

---

## Requisitos

### Hardware

- Raspberry Pi 3 (ou superior) com Bluetooth embutido ou adaptador compatível.

### Software

- Linux com **BlueZ** e D-Bus
- Python 3.8+ recomendado
- Dependências Python:
  - [`dbus-next`](https://pypi.org/project/dbus-next/)

---

## Instalação

Clone o repositório:

```bash
git clone https://github.com/seu-usuario/bt-ble-file.git
cd bt-ble-file
