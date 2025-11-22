#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi 3 - BLE ONLY periférico para RECEBER ARQUIVOS (sem forçar pareamento)
- Apenas BLE (o script não altera BR/EDR, Discoverable ou Pairable)
- GATT Server (serviço estilo Nordic UART: RX=Write, TX=Notify)
- LE Advertising start/stop via teclado
- Agent NoInputNoOutput registrado (apenas se algum cliente tentar parear),
- Segurança deve ser feita na camada de aplicação (ex.: criptografia do payload)

"""

import asyncio
import sys
import os
import tty
import termios
import json
import time
from collections import deque
from typing import Dict, Any, List

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method, dbus_property
from dbus_next.constants import PropertyAccess, BusType
from dbus_next import Variant

BLUEZ = "org.bluez"
OBJMGR_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
ADAPTER_IFACE = "org.bluez.Adapter1"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADV_MGR_IFACE = "org.bluez.LEAdvertisingManager1"
AGENT_MGR_IFACE = "org.bluez.AgentManager1"

AGENT_PATH = "/tiago/agent"
APP_ROOT = "/tiago/gatt"
ADV_PATH = "/tiago/adv0"
ADAPTER_FALLBACK = "/org/bluez/hci0"

# Serviço estilo "Nordic UART"
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write, WriteWithoutResponse
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notify (Read opcional)

# Nome que aparecerá no scan BLE
LOCAL_NAME = "Pi-BLE-UART"

DOWNLOAD_DIR = os.path.abspath("./downloads")


# ---------- Util: leitura de 1 tecla sem bloquear ----------
class KeyReader:
    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

    def restore(self):
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


# ---------- Agent silencioso (pareamento Just Works) ----------
class NoIOAgent(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self) -> None:
        return

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u') -> None:
        return

    @method()
    def RequestAuthorization(self, device: 'o') -> None:
        return

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's') -> None:
        return

    @method()
    def Cancel(self) -> None:
        return

    @method()
    def RequestPinCode(self, device: 'o') -> 's':
        raise Exception("org.bluez.Error.Rejected")

    @method()
    def DisplayPinCode(self, device: 'o', pincode: 's') -> None:
        return

    @method()
    def RequestPasskey(self, device: 'o') -> 'u':
        raise Exception("org.bluez.Error.Rejected")

    @method()
    def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q') -> None:
        return


# ---------- GATT: Característica ----------
class GattCharacteristic(ServiceInterface):
    def __init__(self, uuid: str, flags: List[str], service_path: str):
        super().__init__("org.bluez.GattCharacteristic1")
        self.uuid = uuid
        self.flags = flags
        self.service_path = service_path
        self._value = bytes()
        self._notifying = False
        self.received_cb = None

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> 's':
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> 'o':
        return self.service_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> 'as':
        return self.flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> 'ay':
        return self._value

    @method()
    def ReadValue(self, options: 'a{sv}') -> 'ay':
        return self._value

    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}') -> None:
        incoming = bytes(value)
        self._value = incoming
        try:
            as_text = self._value.decode("utf-8", errors="replace")
        except Exception:
            as_text = ""
        print(f"[RX/WriteValue] len={len(incoming)} | {self.uuid} | {as_text[:80]!r}")
        if self.received_cb:
            try:
                # trata cada quadro separadamente
                self.received_cb(incoming)
            except Exception as e:
                print(f"[RX/Callback ERR] {e}")

    @method()
    def StartNotify(self) -> None:
        self._notifying = True

    @method()
    def StopNotify(self) -> None:
        self._notifying = False

    def notify(self, data: bytes):
        self._value = data or b""
        if self._notifying:
            self.emit_properties_changed({"Value": self._value}, [])


# ---------- Serviço ----------
class GattService(ServiceInterface):
    def __init__(self, uuid: str, primary: bool, path: str):
        super().__init__("org.bluez.GattService1")
        self.uuid = uuid
        self.primary = primary
        self.path = path

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> 's':
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> 'b':
        return self.primary


# ---------- LE Advertisement ----------
class LEAdvertisement(ServiceInterface):
    def __init__(self, local_name: str, service_uuids: List[str]):
        super().__init__("org.bluez.LEAdvertisement1")
        self.local_name = local_name
        self.service_uuids = service_uuids

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> 's':
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> 's':
        return self.local_name

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> 'as':
        return self.service_uuids

    @dbus_property(access=PropertyAccess.READ)
    def TxPower(self) -> 'n':
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> 'a{qv}':
        return {}

    @dbus_property(access=PropertyAccess.READ)
    def ServiceData(self) -> 'a{sv}':
        return {}

    @method()
    def Release(self) -> None:
        return


def _safe_filename(name: str) -> str:
    # só tira barras e null, o resto mantém
    name = os.path.basename(name).strip().replace("\x00", "")
    return name or "arquivo.bin"


# ---------- App ----------
class BLEApp:
    def __init__(self):
        self.bus = None
        self.adapter_path = None
        self.props_iface = None

        self.service = None
        self.rx_char = None
        self.tx_char = None

        self.adv = None
        self.advertising = False

        self.rx_buffer = deque(maxlen=1000)

        # Controle de recebimento de arquivo
        self.recv_enabled = True
        self.recv_active = False
        self.recv_name = None
        self.recv_size = 0
        self.recv_bytes = None
        self.recv_chunks = 0
        self.recv_t0 = 0.0
        self.recv_last_saved_path = None

        self._shutdown = asyncio.Event()

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async def connect_bus(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def get_managed_objects(self):
        obj = await self.bus.introspect(BLUEZ, "/")
        mgr = self.bus.get_proxy_object(BLUEZ, "/", obj).get_interface(OBJMGR_IFACE)
        return await mgr.call_get_managed_objects()

    async def resolve_adapter(self):
        objs = await self.get_managed_objects()
        for path, ifaces in objs.items():
            if ADAPTER_IFACE in ifaces:
                self.adapter_path = path
                break
        if not self.adapter_path:
            self.adapter_path = ADAPTER_FALLBACK

        aobj = await self.bus.introspect(BLUEZ, self.adapter_path)
        pobj = self.bus.get_proxy_object(BLUEZ, self.adapter_path, aobj)
        self.props_iface = pobj.get_interface(PROPS_IFACE)

    async def register_agent(self):
        agent = NoIOAgent()
        self.bus.export(AGENT_PATH, agent)
        obj = await self.bus.introspect(BLUEZ, "/org/bluez")
        mgr = self.bus.get_proxy_object(BLUEZ, "/org/bluez", obj).get_interface(AGENT_MGR_IFACE)
        await mgr.call_register_agent(AGENT_PATH, "NoInputNoOutput")
        await mgr.call_request_default_agent(AGENT_PATH)
        print("[AGENT] Agente NoInputNoOutput registrado (fluxo normal sem pareamento).")

    async def register_gatt(self):
        service_path = f"{APP_ROOT}/service0"
        rx_path = f"{service_path}/char0"
        tx_path = f"{service_path}/char1"

        self.service = GattService(NUS_SERVICE_UUID, True, service_path)
        self.bus.export(service_path, self.service)

        # RX/TX sem encrypt-* para não forçar bonding no Pi 3
        self.rx_char = GattCharacteristic(
            NUS_RX_CHAR_UUID,
            ["write", "write-without-response"],
            service_path,
        )
        self.rx_char.received_cb = self._on_rx
        self.bus.export(rx_path, self.rx_char)

        self.tx_char = GattCharacteristic(
            NUS_TX_CHAR_UUID,
            ["notify", "read"],
            service_path,
        )
        self.bus.export(tx_path, self.tx_char)

        aobj = await self.bus.introspect(BLUEZ, self.adapter_path)
        gatt_mgr = self.bus.get_proxy_object(BLUEZ, self.adapter_path, aobj).get_interface(GATT_MANAGER_IFACE)
        await gatt_mgr.call_register_application(APP_ROOT, {})
        print(f"[GATT] Serviço NUS registrado. LocalName: {LOCAL_NAME}")
        print("[GATT] RX/TX sem encrypt-*, sem pareamento obrigatório (BLE ONLY).")

    def _on_rx(self, data: bytes):
        self.rx_buffer.append(data)

        # Tenta decodificar como JSON
        try:
            text = data.decode("utf-8")
            payload = json.loads(text)
            op = (payload.get("op") or "").lower()
        except Exception:
            op = None

        # === 1) Mensagem JSON: controle ===
        if op == "file_begin":
            if not self.recv_enabled:
                print("[FILE] file_begin recebido, mas recepção está OFF (ignorado).")
                return

            self.recv_active = True
            self.recv_name = _safe_filename(payload.get("name", "arquivo.bin"))
            self.recv_size = int(payload.get("size", 0))
            self.recv_bytes = bytearray()
            self.recv_chunks = 0
            self.recv_t0 = time.perf_counter()

            print(f"[FILE] BEGIN '{self.recv_name}' size={self.recv_size}")
            return

        elif op == "file_end":
            if not (self.recv_enabled and self.recv_active and self.recv_bytes is not None):
                print("[FILE] file_end ignorado (sem begin ativo).")
                return

            elapsed = (time.perf_counter() - self.recv_t0) * 1000.0
            path = os.path.join(DOWNLOAD_DIR, self.recv_name or "arquivo.bin")

            if len(self.recv_bytes) == 0:
                print("[FILE] Nada recebido entre begin/end, abortando.")
                self.recv_active = False
                return

            with open(path, "wb") as f:
                f.write(self.recv_bytes)

            print(f"[FILE] END saved={len(self.recv_bytes)} bytes path={path}")
            print(f"[FILE] STATS chunks={self.recv_chunks} elapsed_ms={elapsed:.0f}")
            if self.recv_size > 0 and len(self.recv_bytes) != self.recv_size:
                print(f"[FILE] WARN tamanho esperado={self.recv_size} bytes, recebido={len(self.recv_bytes)} bytes")

            self.recv_last_saved_path = path
            self.recv_active = False
            self.recv_name = None
            self.recv_bytes = None
            return

        # === 2) Dados binários: conteúdo do arquivo ===
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

    async def start_advertising(self):
        if self.advertising:
            print("[ADV] Já está anunciando.")
            return
        self.adv = LEAdvertisement(LOCAL_NAME, [NUS_SERVICE_UUID])
        self.bus.export(ADV_PATH, self.adv)
        aobj = await self.bus.introspect(BLUEZ, self.adapter_path)
        adv_mgr = self.bus.get_proxy_object(BLUEZ, self.adapter_path, aobj).get_interface(LE_ADV_MGR_IFACE)
        await adv_mgr.call_register_advertisement(ADV_PATH, {})
        self.advertising = True
        print("[ADV] Advertising BLE iniciado.")

    async def stop_advertising(self):
        if not self.advertising:
            return
        aobj = await self.bus.introspect(BLUEZ, self.adapter_path)
        adv_mgr = self.bus.get_proxy_object(BLUEZ, self.adapter_path, aobj).get_interface(LE_ADV_MGR_IFACE)
        await adv_mgr.call_unregister_advertisement(ADV_PATH)
        self.bus.unexport(ADV_PATH, self.adv)
        self.advertising = False
        print("[ADV] Advertising parado.")

    async def run(self):
        await self.connect_bus()
        await self.resolve_adapter()
        print(f"[SYS] Adapter: {self.adapter_path}")
        print("[SYS] BLE ONLY: script não mexe em BR/EDR, Discoverable ou Pairable.")

        await self.register_agent()
        await self.register_gatt()
        await self.start_advertising()

        print_controls()

        reader = KeyReader()
        loop = asyncio.get_running_loop()

        def on_stdin_byte():
            try:
                ch = os.read(reader._fd, 1).decode(errors="ignore")
            except Exception:
                ch = ""
            if ch:
                asyncio.create_task(self.handle_key(ch))

        loop.add_reader(reader._fd, on_stdin_byte)
        await self._shutdown.wait()
        loop.remove_reader(reader._fd)
        reader.restore()

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


def print_controls():
    print(f"""
[CONTROLES]
  a = Start Advertising (BLE) · Nome: {LOCAL_NAME}
  s = Stop Advertising
  e = Alternar 'receber arquivo' ON/OFF
  v = Mostrar caminho do último arquivo salvo
  q = Sair

[INFO]
  - Este script é BLE ONLY: não força pareamento, não altera BR/EDR.
  - Segurança/autenticação devem ser feitas na camada de aplicação
    (ex.: criptografando o conteúdo do arquivo antes de enviar).
""")


if __name__ == "__main__":
    try:
        asyncio.run(BLEApp().run())
    except KeyboardInterrupt:
        pass
