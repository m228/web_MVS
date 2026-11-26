"""Эмулятор OPC UA-сервера аппарата для отладки без железа (живого сервера у нас нет).

Теги как у вакуум-аппарата: Objects → VA → Brix (СВ гуляет 80–90), T_app, Level, Current, P_top, P_bot, Mode (стадия 1–21),
Submode, TCooking, клапаны V_syrup / V_water / V_product; структура VA → Recipe (вложенные теги) и тег Broken с плохим
статусом (как «нет связи сервера с ПЛК»). Сервер только отдаёт значения — писать в него приложению незачем.

Запуск (из корня проекта):
    .venv\\Scripts\\python scripts\\opcua_sim.py                         без защиты,        opc.tcp://127.0.0.1:48400
    .venv\\Scripts\\python scripts\\opcua_sim.py --secure --user op --password 123
                                                               + Basic256Sha256 Sign/SignAndEncrypt и вход по логину

Из тестов: start_sim(port, secure, users) → объект со .url, .stop(), .set(name, value).
"""
import argparse
import asyncio
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AS_BLOCKS = 600                              # блоков в папке PCS/AS05 — больше, чем дерево показывает за раз (BROWSE_LIMIT)
TAGS = {                                     # имя тега → (начальное значение, тип OPC UA)
    "Brix": (86.4, "Float"),
    "T_app": (71.2, "Float"),
    "Level": (62.0, "Float"),
    "Current": (31.0, "Float"),
    "P_top": (-0.78, "Float"),
    "P_bot": (0.35, "Float"),
    "Mode": (5, "Int16"),
    "Submode": (2, "Int16"),
    "TCooking": (1234, "UInt32"),
    "V_syrup": (40.0, "Float"),
    "V_water": (0.0, "Float"),
    "V_product": (100.0, "Float"),
}


class Sim:
    def __init__(self, port=48400, secure=False, users=None, host="127.0.0.1", animate=True):
        self.url = "opc.tcp://%s:%d/va" % (host, port)
        self.port, self.secure, self.users, self.host, self.animate = port, secure, users or {}, host, animate
        self._ready, self._halt = threading.Event(), threading.Event()
        self._vars, self._loop, self._err = {}, None, None
        self.ns = None
        self._th = threading.Thread(target=self._run, name="opcua-sim", daemon=True)

    def start(self, timeout=30):
        self._th.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("эмулятор OPC UA не поднялся: %s" % (self._err,))
        if self._err:
            raise RuntimeError("эмулятор OPC UA: %s" % self._err)
        return self

    def stop(self):
        self._halt.set()
        self._th.join(10)

    def node_id(self, name):
        return "ns=%d;s=VA.%s" % (self.ns, name)

    def set(self, name, value):
        """Поменять значение тега (из теста)."""
        from asyncua import ua
        var, vtype = self._vars[name]
        fut = asyncio.run_coroutine_threadsafe(var.write_value(ua.Variant(value, getattr(ua.VariantType, vtype))), self._loop)
        fut.result(5)

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:                          # pragma: no cover — видно в start()
            self._err = e
            self._ready.set()

    async def _main(self):
        import asyncua
        from asyncua import ua
        from asyncua.crypto.permission_rules import User, UserRole
        self._loop = asyncio.get_running_loop()
        users = self.users

        class Users:
            def get_user(self, iserver, username=None, password=None, certificate=None):
                if username is None:
                    return None if users else User(role=UserRole.User)    # с логинами — анонимно нельзя
                return User(role=UserRole.User) if users.get(username) == password else None

        srv = asyncua.Server(user_manager=Users())
        await srv.init()
        srv.set_endpoint("opc.tcp://%s:%d/va" % (self.host, self.port))
        srv.set_server_name("web_MVS OPC UA simulator")
        await srv.set_application_uri("urn:web_mvs:sim")          # тот же URI, что в сертификате сервера
        if self.secure:
            await self._load_cert(srv)
            srv.set_security_policy([ua.SecurityPolicyType.NoSecurity, ua.SecurityPolicyType.Basic256Sha256_Sign,
                                     ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
        else:
            srv.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await srv.register_namespace("urn:web_mvs:sim")
        ns = self.ns
        va = await srv.nodes.objects.add_folder(ua.NodeId("VA", ns), "VA")
        for name, (val, vtype) in TAGS.items():
            var = await va.add_variable(ua.NodeId("VA." + name, ns), name, ua.Variant(val, getattr(ua.VariantType, vtype)))
            self._vars[name] = (var, vtype)
        recipe = await va.add_object(ua.NodeId("VA.Recipe", ns), "Recipe")
        await recipe.add_variable(ua.NodeId("VA.Recipe.SV_target", ns), "SV_target", ua.Variant(91.5, ua.VariantType.Float))
        broken = await va.add_variable(ua.NodeId("VA.Broken", ns), "Broken", ua.Variant(0.0, ua.VariantType.Float))
        await broken.write_value(ua.DataValue(ua.Variant(0.0, ua.VariantType.Float), StatusCode_=ua.StatusCode(ua.StatusCodes.BadNotConnected)))
        # Как у Siemens PCS7: AS с сотнями блоков (сервер отдаёт их не по алфавиту), один и тот же узел по двум ссылкам
        # (Organizes + HasNotifier) и два разных узла с одинаковым именем.
        pcs = await srv.nodes.objects.add_folder(ua.NodeId("PCS", ns), "PCS")
        as05 = await pcs.add_folder(ua.NodeId("PCS.AS05", ns), "AS05")
        for i in range(AS_BLOCKS - 1, -1, -1):
            await as05.add_folder(ua.NodeId("PCS.AS05.B%03d" % i, ns), ua.QualifiedName("A_PRODUCT/PA%03d" % (i + 100), ns))
        pa012 = await as05.add_folder(ua.NodeId("PCS.AS05.PA012", ns), ua.QualifiedName("A_PRODUCT/APRODUCT/PA012", ns))
        await pa012.add_variable(ua.NodeId("PCS.AS05.PA012.PV", ns), ua.QualifiedName("PA012.PV_In#Value", ns), ua.Variant(42.5, ua.VariantType.Float))
        oss = await pcs.add_object(ua.NodeId("PCS.OSS01", ns), ua.QualifiedName("OSS_OSS01::", ns))
        await pcs.add_reference(oss.nodeid, ua.ObjectIds.HasNotifier, forward=True, bidirectional=False)
        for k in ("A", "B"):
            await pcs.add_object(ua.NodeId("PCS.OSS05" + k, ns), ua.QualifiedName("OSS_OSS05::", ns))
        async with srv:
            self._ready.set()
            t0 = time.time()
            while not self._halt.is_set():
                await asyncio.sleep(0.2)
                if self.animate:
                    t = time.time() - t0
                    await self._vars["Brix"][0].write_value(ua.Variant(85.0 + 5.0 * math.sin(t / 30.0), ua.VariantType.Float))
                    await self._vars["TCooking"][0].write_value(ua.Variant(1234 + int(t), ua.VariantType.UInt32))

    async def _load_cert(self, srv):
        from cryptography.x509.oid import ExtendedKeyUsageOID
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        d = Path(tempfile.gettempdir()) / "web_mvs_opcua_sim"
        d.mkdir(exist_ok=True)
        key, cert = d / "sim_key.pem", d / "sim_cert.der"
        await setup_self_signed_certificate(key, cert, "urn:web_mvs:sim", "localhost", [ExtendedKeyUsageOID.SERVER_AUTH],
                                            {"commonName": "web_MVS sim", "organizationName": "Maximum Automation"})
        await srv.load_certificate(str(cert))
        await srv.load_private_key(str(key))


def start_sim(port=48400, secure=False, users=None, host="127.0.0.1", animate=True):
    return Sim(port, secure, users, host, animate).start()


def main():
    ap = argparse.ArgumentParser(description="Эмулятор OPC UA-сервера аппарата")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=48400)
    ap.add_argument("--secure", action="store_true", help="добавить Basic256Sha256 Sign / SignAndEncrypt")
    ap.add_argument("--user", default="", help="логин (тогда анонимный вход запрещён)")
    ap.add_argument("--password", default="")
    a = ap.parse_args()
    import logging
    logging.basicConfig(level=logging.WARNING)
    sim = start_sim(a.port, a.secure, {a.user: a.password} if a.user else None, a.host)
    print("OPC UA эмулятор: %s  (теги %s)  Ctrl+C — стоп" % (sim.url, sim.node_id("Brix")))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
