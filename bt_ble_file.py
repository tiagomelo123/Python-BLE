#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BLE File Receiver + Controls
- Nordic UART Service (RX write, TX notify)
- Start/Stop Advertising
- Discoverable ON/OFF
- Pairable ON/OFF
- Agent NoInputNoOutput (pareamento automático)
- Recebimento de arquivo via JSON (file_begin/file_chunk/file_end)
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

# ==============================
# BlueZ constants
# ==============================
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

# Nordic UART Service
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

DOWNLOAD_DIR = os.path.abspath("./downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==============================
# Key Reader
# ==============================
class KeyReader:
    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

    def restore(self):
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

# ==============================
# Agent NoInputNoOutput
# ==============================
class NoIOAgent(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method() def Release(self): pass
    @method() def RequestConfirmation(self, device:'o', passkey:'u'): pass
    @method() def RequestAuthorization(self, device:'o'): pass
    @method() def AuthorizeService(self, device:'o', uuid:'s'): pass
    @method() def Cancel(self): pass

    @method() 
    def RequestPinCode(self, device:'o') -> 's':
        raise Exception("org.bluez.Error.Rejected")

    @method() 
    def RequestPasskey(self, device:'o') -> 'u':
        raise Exception("org.bluez.Error.Rejected")

    @method() 
    def DisplayPasskey(self, device:'o', passkey:'u', entered:'q'): pass

    @method() 
    def DisplayPinCode(self, device:'o', pincode:'s'): pass

# ==============================
# GATT Characteristic
# ==============================
class GattCharacteristic(ServiceInterface):
    def __init__(self, uuid, flags, service_path):
        super().__init__("org.bluez.GattCharacteristic1")
        self.uuid = uuid
        self.flags = flags
        self.service_path = service_path
        self._value = b""
        self._notifying = False
        self.received_cb = None

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self): return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self): return self.service_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self): return self.flags

    @method()
    def ReadValue(self, options): return self._value

    @method()
    def WriteValue(self, value, options):
        incoming = bytes(value)
        self._value = incoming

        # Log curto
        try: txt = incoming.decode("utf-8", errors="replace")
        except: txt = ""
        print(f"[RX] {len(incoming)} bytes | {txt[:80]!r}")

        # envia ao processamento
        if self.received_cb:
            try:
                self.received_cb(incoming)
            except Exception as e:
                print("[RX CALLBACK ERROR]", e)

    @method()
    def StartNotify(self): self._notifying = True

    @method()
    def StopNotify(self): self._notifying = False

    def notify(self, data:bytes):
        self._value = data or b""
        if self._notifying:
            self.emit_properties_changed({"Value": self._value}, [])

# ==============================
# GATT Service
# ==============================
class GattService(ServiceInterface):
    def __init__(self, uuid, primary, path):
        super().__init__("org.bluez.GattService1")
        self.uuid = uuid
        self.primary = primary
        self.path = path

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self): return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self): return self.primary

# ==============================
# Advertisement
# ==============================
class LEAdvertisement(ServiceInterface):
    def __init__(self, name, uuids):
        super().__init__("org.bluez.LEAdvertisement1")
        self.name = name
        self.uuids = uuids

    @dbus_property(access=PropertyAccess.READ)
    def Type(self): return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self): return self.name

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self): return self.uuids

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self): return {}

    @dbus_property(access=PropertyAccess.READ)
    def ServiceData(self): return {}

    @dbus_property(access=PropertyAccess.READ)
    def TxPower(self): return 0

    @method()
    def Release(self): pass

# ==============================
# Helpers
# ==============================
def _safe_filename(name):
    name = os.path.basename(name.strip().replace("\x00",""))
    return name or "arquivo.bin"

# ==============================
# BLE App
# ==============================
class BLEApp:
    def __init__(self):
        self.bus = None
        self.adapter = None
        self.props_iface = None
        self.rx_char = None
        self.tx_char = None
        self.adv = None
        self.advertising = False

        # File transfer
        self.recv_enabled = True
        self.recv_active = False
        self.recv_name = None
        self.recv_size = 0
        self.recv_bytes = None
        self.recv_chunks = 0
        self.recv_t0 = 0
        self.recv_last_saved = None

        self._shutdown = asyncio.Event()

    # ------------------------------
    # DBus
    # ------------------------------
    async def connect(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def resolve_adapter(self):
        obj = await self.bus.introspect(BLUEZ, "/")
        mgr = self.bus.get_proxy_object(BLUEZ, "/", obj).get_interface(OBJMGR_IFACE)
        objs = await mgr.call_get_managed_objects()

        for path, ifs in objs.items():
            if ADAPTER_IFACE in ifs:
                self.adapter = path
                break

        if not self.adapter:
            self.adapter = ADAPTER_FALLBACK

        aobj = await self.bus.introspect(BLUEZ, self.adapter)
        pobj = self.bus.get_proxy_object(BLUEZ, self.adapter, aobj)
        self.props_iface = pobj.get_interface(PROPS_IFACE)

    # ------------------------------
    # Adapter Controls
    # ------------------------------
    async def discoverable_on(self):
        await self.props_iface.call_set(ADAPTER_IFACE,"Discoverable",Variant("b",True))
        print("[ADAPTER] Discoverable ON")

    async def discoverable_off(self):
        await self.props_iface.call_set(ADAPTER_IFACE,"Discoverable",Variant("b",False))
        print("[ADAPTER] Discoverable OFF")

    async def pairable_on(self):
        await self.props_iface.call_set(ADAPTER_IFACE,"Pairable",Variant("b",True))
        print("[ADAPTER] Pairable ON")

    async def pairable_off(self):
        await self.props_iface.call_set(ADAPTER_IFACE,"Pairable",Variant("b",False))
        print("[ADAPTER] Pairable OFF")

    # ------------------------------
    # Agent
    # ------------------------------
    async def register_agent(self):
        agent = NoIOAgent()
        self.bus.export(AGENT_PATH, agent)
        obj = await self.bus.introspect(BLUEZ, "/org/bluez")
        mgr = self.bus.get_proxy_object(BLUEZ,"/org/bluez", obj).get_interface(AGENT_MGR_IFACE)
        await mgr.call_register_agent(AGENT_PATH,"NoInputNoOutput")
        await mgr.call_request_default_agent(AGENT_PATH)
        print("[AGENT] Agent NoInputNoOutput registrado.")

    # ------------------------------
    # GATT Setup
    # ------------------------------
    async def register_gatt(self):
        # service
        srv_path = f"{APP_ROOT}/service0"
        service = GattService(NUS_SERVICE_UUID, True, srv_path)
        self.bus.export(srv_path, service)

        # RX
        rx_path = f"{srv_path}/rx"
        self.rx_char = GattCharacteristic(NUS_RX_CHAR_UUID,
                                          ["write","write-without-response"],
                                          srv_path)
        self.rx_char.received_cb = self.on_rx
        self.bus.export(rx_path, self.rx_char)

        # TX
        tx_path = f"{srv_path}/tx"
        self.tx_char = GattCharacteristic(NUS_TX_CHAR_UUID, ["notify","read"], srv_path)
        self.bus.export(tx_path, self.tx_char)

        # register
        aobj = await self.bus.introspect(BLUEZ, self.adapter)
        gatt = self.bus.get_proxy_object(BLUEZ, self.adapter, aobj).get_interface(GATT_MANAGER_IFACE)
        await gatt.call_register_application(APP_ROOT, {})
        print("[GATT] Serviço NUS registrado.")

    # ------------------------------
    # Advertisement
    # ------------------------------
    async def start_advertising(self):
        if self.advertising:
            print("[ADV] Já anunciando.")
            return
        self.adv = LEAdvertisement("Pi-BLE-UART",[NUS_SERVICE_UUID])
        self.bus.export(ADV_PATH, self.adv)

        aobj = await self.bus.introspect(BLUEZ, self.adapter)
        adv_mgr = self.bus.get_proxy_object(BLUEZ, self.adapter, aobj).get_interface(LE_ADV_MGR_IFACE)
        await adv_mgr.call_register_advertisement(ADV_PATH,{})
        self.advertising = True
        print("[ADV] Advertising iniciado.")

    async def stop_advertising(self):
        if not self.advertising:
            return
        aobj = await self.bus.introspect(BLUEZ, self.adapter)
        adv_mgr = self.bus.get_proxy_object(BLUEZ,self.adapter,aobj).get_interface(LE_ADV_MGR_IFACE)
        await adv_mgr.call_unregister_advertisement(ADV_PATH)
        self.bus.unexport(ADV_PATH, self.adv)
        self.advertising = False
        print("[ADV] Advertising parado.")

    # ------------------------------
    # TX notify
    # ------------------------------
    async def send_tx(self, text:str):
        if not self.tx_char:
            print("[TX] TX indisponível")
            return
        data = text.encode()
        self.tx_char.notify(data)
        print("[TX]", data)

    # ------------------------------
    # RX Handler (file receive)
    # ------------------------------
    def on_rx(self,data:bytes):
        # tenta JSON
        try:
            txt = data.decode("utf-8")
            payload = json.loads(txt)
            op = (payload.get("op") or "").lower()
        except:
            op = None

        # ---------------- begin
        if op == "file_begin":
            if not self.recv_enabled:
                print("[FILE] Recebimento OFF")
                return

            self.recv_active = True
            self.recv_name = _safe_filename(payload.get("name","arquivo.bin"))
            self.recv_size = int(payload.get("size",0))
            self.recv_bytes = bytearray()
            self.recv_chunks = 0
            self.recv_t0 = time.perf_counter()
            print(f"[FILE] BEGIN '{self.recv_name}' size={self.recv_size}")
            return

        # ---------------- chunk
        elif op == "file_chunk":
            if not (self.recv_active and self.recv_bytes is not None):
                return

            b64 = payload.get("b64") or payload.get("data")
            if not isinstance(b64,str):
                return

            try:
                chunk = base64.b64decode(b64)
                self.recv_bytes.extend(chunk)
                self.recv_chunks += 1
            except Exception as e:
                print("[FILE] erro chunk:", e)
            return

        # ---------------- end
        elif op == "file_end":
            if not (self.recv_active and self.recv_bytes):
                print("[FILE] file_end sem dados")
                return

            elapsed = (time.perf_counter()-self.recv_t0)*1000
            path = os.path.join(DOWNLOAD_DIR, self.recv_name)

            with open(path,"wb") as f:
                f.write(self.recv_bytes)

            print(f"[FILE] END saved={len(self.recv_bytes)} bytes path={path}")
            print(f"[FILE] STATS chunks={self.recv_chunks} elapsed_ms={elapsed:.0f}")
            self.recv_last_saved = path

            # reset
            self.recv_active = False
            self.recv_name = None
            self.recv_bytes = None
            return

        # ---------------- se não for JSON → ignora
        else:
            return

    # ------------------------------
    # Key handler
    # ------------------------------
    async def handle_key(self,ch):
        ch = ch.strip().lower()
        if ch=="a": await self.start_advertising()
        elif ch=="s": await self.stop_advertising()
        elif ch=="d": await self.discoverable_on()
        elif ch=="f": await self.discoverable_off()
        elif ch=="p": await self.pairable_on()
        elif ch=="o": await self.pairable_off()
        elif ch=="t": await self.send_tx("Hello from Pi")
        elif ch=="v": print("[FILE] Último salvo:", self.recv_last_saved or "-")
        elif ch=="e":
            self.recv_enabled = not self.recv_enabled
            print("[FILE] Receber arquivo:", "ON" if self.recv_enabled else "OFF")
        elif ch=="q":
            print("[SYS] Encerrando…")
            self._shutdown.set()

    # ------------------------------
    # Loop principal
    # ------------------------------
    async def run(self):
        await self.connect()
        await self.resolve_adapter()
        await self.register_agent()
        await self.register_gatt()
        await self.start_advertising()
        await self.pairable_on()
        await self.discoverable_on()

        print_controls()

        reader = KeyReader()
        loop = asyncio.get_running_loop()

        loop.add_reader(reader._fd,
            lambda: asyncio.create_task(self.handle_key(
                os.read(reader._fd,1).decode(errors="ignore"))))

        await self._shutdown.wait()
        loop.remove_reader(reader._fd)
        reader.restore()

# ==============================
# Controls Text
# ==============================
def print_controls():
    print("""
[CONTROLES BLUETOOTH]
  a = Start Advertising (visível no BLE)
  s = Stop Advertising
  d = Adapter Discoverable ON
  f = Adapter Discoverable OFF
  p = Pairable ON
  o = Pairable OFF
  t = Enviar notificação 'Hello from Pi'
  e = Alternar receber arquivo ON/OFF
  v = Mostrar último arquivo salvo
  q = Sair
""")

# ==============================
# MAIN
# ==============================
if __name__ == "__main__":
    try:
        asyncio.run(BLEApp().run())
    except KeyboardInterrupt:
        pass
