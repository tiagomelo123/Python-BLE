#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Raspberry Pi 3 - BLE periférico com controle por teclado + pareamento silencioso
- GATT Server (serviço estilo Nordic UART: RX=Write, TX=Notify)
- LE Advertising start/stop via teclado
- Adapter Discoverable/Pairable toggles
- Agent NoInputNoOutput (pareamento silencioso/Just Works)
- Modo de recebimento de arquivo via JSON:
    { "op":"file_begin","name":"arquivo.ext","size":1234 }
    <dados binários puros em vários writes>
    { "op":"file_end" }
Execute como root: sudo -E python3 bt_ble_file.py
"""

import asyncio
import sys
import os
import tty
import termios
import json
import base64
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
        self.bus: MessageBus | None = None
        self.adapter_path = None
        self.props_iface = None
        self.service = None
        self.rx_char: GattCharacteristic | None = None
        self.tx_char: GattCharacteristic | None = None
        self.adv = None
        self.advertising = False

        self.rx_buffer = deque(maxlen=1000)

        # recebimento de arquivo
        self.recv_enabled = True
        self.recv_active = False
        self.recv_name = None
        self.recv_size = 0
        self.recv_bytes: bytearray | None = None
        self.recv_chunks = 0
        self.recv_t0 = 0.0
        self.recv_last_saved_path = None

        self._shutdown = asyncio.Event()
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # ----- DBus / Adapter -----
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

    # ----- Adapter props (Discoverable / Pairable) -----
    async def set_adapter_prop(self, iface: str, prop: str, variant: Variant):
        await self.props_iface.call_set(iface, prop, variant)

    async def discoverable_on(self):
        await self.set_adapter_prop(ADAPTER_IFACE, "Discoverable", Variant("b", True))

    async def discoverable_off(self):
        await self.set_adapter_prop(ADAPTER_IFACE, "Discoverable", Variant("b", False))

    async def pairable_on(self):
        await self.set_adapter_prop(ADAPTER_IFACE, "Pairable", Variant("b", True))

    async def pairable_off(self):
        await self.set_adapter_prop(ADAPTER_IFACE, "Pairable", Variant("b", False))

    # ----- Agent -----
    async def register_agent(self):
        agent = NoIOAgent()
        self.bus.export(AGENT_PATH, agent)
        obj = await self.bus.introspect(BLUEZ, "/org/bluez")
        mgr = self.bus.get_proxy_object(BLUEZ, "/org/bluez", obj).get_interface(AGENT_MGR_IFACE)
        await mgr.call_register_agent(AGENT_PATH, "NoInputNoOutput")
        await mgr.call_request_default_agent(AGENT_PATH)
        print("[AGENT] Agente NoInputNoOutput registrado como padrão.")

    # ----- GATT -----
    async def register_gatt(self):
        self.service = GattService(NUS_SERVICE_UUID, True, f"{APP_ROOT}/service0")
        self.bus.export(f"{APP_ROOT}/service0", self.service)

        self.rx_char = GattCharacteristic(
            NUS_RX_CHAR_UUID,
            ["write", "write-without-response"],
            f"{APP_ROOT}/service0",
        )
        self.rx_char.received_cb = self._on_rx
        self.bus.export(f"{APP_ROOT}/service0/char0", self.rx_char)

        self.tx_char = GattCharacteristic(
            NUS_TX_CHAR_UUID,
            ["notify", "read"],
            f"{APP_ROOT}/service0",
        )
        self.bus.export(f"{APP_ROOT}/service0/char1", self.tx_char)

        aobj = await self.bus.introspect(BLUEZ, self.adapter_path)
        gatt_mgr = self.bus.get_proxy_object(BLUEZ, self.adapter_path, aobj).get_interface(GATT_MANAGER_IFACE)
        await gatt_mgr.call_register_application(APP_ROOT, {})
        print("[GATT] Serviço NUS registrado.")

    async def notify_tx(self, payload: bytes):
        if not self.tx_char:
            print("[TX] Característica TX indisponível.")
            return
        self.tx_char.notify(payload)
        print(f"[TX] Notificado: {payload!r}")

    # ----- RX -----
    def _on_rx(self, data: bytes):
        self.rx_buffer.append(data)

        # tenta JSON (controle)
        try:
            text = data.decode("utf-8")
            payload = json.loads(text)
            op = (payload.get("op") or "").lower()
        except Exception:
            op = None

        # 1) Controle de arquivo
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

        elif op == "file_end":
            if not (self.recv_enabled and self.recv_active and self.recv_bytes is not None):
                print("[FILE] file_end ignorado (sem begin).")
                return

            elapsed = (time.perf_counter() - self.recv_t0) * 1000.0
            path = os.path.join(DOWNLOAD_DIR, self.recv_name or "arquivo.bin")

            if len(self.recv_bytes) == 0:
                print("[FILE] Nada recebido entre begin/end, abortando.")
                self.recv_active = False
                self.recv_name = None
                self.recv_bytes = None
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

        # 2) Dados binários (quando estamos em modo de arquivo)
        if not (self.recv_active and self.recv_bytes is not None):
            # se não estamos recebendo arquivo, apenas log leve
            return

        raw = bytes(data)
        if not raw:
            return

        self.recv_bytes.extend(raw)
        self.recv_chunks += 1

        if self.recv_chunks % 100 == 0:
            print(f"[FILE] chunk #{self.recv_chunks} · total {len(self.recv_bytes)} bytes")

    # ----- Advertising -----
    async def start_advertising(self):
        if self.advertising:
            print("[ADV] Já está anunciando.")
            return
        self.adv = LEAdvertisement("Pi-BLE-UART", [NUS_SERVICE_UUID])
        self.bus.export(ADV_PATH, self.adv)
        aobj = await self.bus.introspect(BLUEZ, self.adapter_path)
        adv_mgr = self.bus.get_proxy_object(BLUEZ, self.adapter_path, aobj).get_interface(LE_ADV_MGR_IFACE)
        await adv_mgr.call_register_advertisement(ADV_PATH, {})
        self.advertising = True
        print("[ADV] Advertising iniciado.")

    async def stop_advertising(self):
        if not self.advertising:
            print("[ADV] Não está anunciando.")
            return
        aobj = await self.bus.introspect(BLUEZ, self.adapter_path)
        adv_mgr = self.bus.get_proxy_object(BLUEZ, self.adapter_path, aobj).get_interface(LE_ADV_MGR_IFACE)
        await adv_mgr.call_unregister_advertisement(ADV_PATH)
        self.bus.unexport(ADV_PATH, self.adv)
        self.advertising = False
        print("[ADV] Advertising parado.")

    # ----- Loop principal -----
    async def run(self):
        await self.connect_bus()
        await self.resolve_adapter()
        await self.register_agent()
        await self.register_gatt()
        await self.pairable_on()          # Pairable ON ao iniciar
        await self.start_advertising()    # já começa anunciando
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

    async def handle_key(self, ch: str):
        ch = ch.strip().lower()
        if ch == "a":
            await self.start_advertising()
        elif ch == "s":
            await self.stop_advertising()
        elif ch == "d":
            await self.discoverable_on()
            print("[ADAPTER] Discoverable=ON")
        elif ch == "f":
            await self.discoverable_off()
            print("[ADAPTER] Discoverable=OFF")
        elif ch == "p":
            await self.pairable_on()
            print("[ADAPTER] Pairable=ON")
        elif ch == "o":
            await self.pairable_off()
            print("[ADAPTER] Pairable=OFF")
        elif ch == "t":
            await self.notify_tx(b"Hello from Pi")
        elif ch == "y":
            msg = input("Digite a mensagem para TX: ")
            await self.notify_tx(msg.encode("utf-8", errors="replace"))
        elif ch == "r":
            print(f"[RX-BUFFER] {len(self.rx_buffer)} itens")
            for i, b in enumerate(list(self.rx_buffer)[-20:], 1):
                try:
                    t = b.decode("utf-8", errors="replace")
                except Exception:
                    t = ""
                print(f"  {i:02d}: {b!r} | '{t}'")
        elif ch == "e":
            self.recv_enabled = not self.recv_enabled
            print(f"[FILE] Receber arquivo: {'ON' if self.recv_enabled else 'OFF'}")
        elif ch == "v":
            print(f"[FILE] Último salvo: {self.recv_last_saved_path or '-'}")
        elif ch == "h":
            print_controls()
        elif ch == "q":
            print("[SYS] Encerrando…")
            self._shutdown.set()


def print_controls():
    print("""
[CONTROLES]
  a = Start Advertising (visível no BLE)
  s = Stop Advertising
  d = Adapter Discoverable ON
  f = Adapter Discoverable OFF
  p = Pairable ON
  o = Pairable OFF
  t = Enviar notificação 'Hello from Pi' na TX
  y = Enviar texto livre pela TX
  r = Mostrar últimos dados recebidos (RX)
  e = Alternar 'receber arquivo' ON/OFF
  v = Mostrar caminho do último arquivo salvo
  h = Ajuda
  q = Sair
""")


if __name__ == "__main__":
    try:
        asyncio.run(BLEApp().run())
    except KeyboardInterrupt:
        pass
