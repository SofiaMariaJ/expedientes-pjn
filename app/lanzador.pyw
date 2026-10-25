"""
Lanzador de Expedientes PJN: antes de abrir el programa se fija si hay una versión nueva
publicada en GitHub y, si la hay, la baja sola. Si no hay internet o algo falla, abre la
versión que ya estaba instalada.
"""

import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REGISTRO = APP_DIR / "registro.txt"
REPO = os.environ.get("PJN_REPO", "https://raw.githubusercontent.com/SofiaMariaJ/expedientes-pjn/main/app/")
LOCAL = APP_DIR / "version.json"

if sys.stdout is None or sys.stderr is None:  # pythonw.exe no tiene consola
    _log = open(REGISTRO, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log


def log(msg):
    print(f"[actualizador] {msg}", flush=True)


def leer_json(texto, por_defecto):
    try:
        return json.loads(texto)
    except Exception:
        return por_defecto


def bajar(url, timeout=20):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "ExpedientesPJN"})
    try:
        import ssl
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        ctx = None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def version_local():
    return leer_json(LOCAL.read_text("utf-8"), {}) if LOCAL.exists() else {}


def instalar_requisitos(requisitos):
    """Instala con pip las librerías nuevas que pida la versión (si falta alguna)."""
    from importlib import metadata
    faltan = []
    for r in requisitos:
        try:
            metadata.version(r)
        except metadata.PackageNotFoundError:
            faltan.append(r)
    if not faltan:
        return True
    python = Path(sys.executable).with_name("python.exe" if os.name == "nt" else Path(sys.executable).name)
    log(f"instalando librerías nuevas: {faltan}")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    r = subprocess.run([str(python), "-m", "pip", "install", "--quiet", *faltan],
                       capture_output=True, text=True, creationflags=flags)
    if r.returncode != 0:
        log(f"pip falló: {r.stderr[-800:]}")
    return r.returncode == 0


def actualizar(aviso):
    """Devuelve True si se instaló una versión nueva."""
    local = version_local()
    try:
        remota = leer_json(bajar(REPO + "version.json").decode("utf-8"), {})
    except Exception as e:
        log(f"sin conexión para buscar actualizaciones ({e}); abro la versión instalada")
        return False
    if int(remota.get("version", 0)) <= int(local.get("version", 0)):
        return False
    if int(remota.get("version", 0)) == int(local.get("saltear", -1)):
        return False  # esa versión ya falló en esta PC: se espera a la siguiente
    aviso(f"Actualizando a la versión {remota['version']}…")
    log(f"versión {local.get('version', '?')} -> {remota['version']}")
    tmp = Path(tempfile.mkdtemp(prefix="pjn_upd_"))
    try:
        for nombre in remota["archivos"]:
            datos = bajar(REPO + nombre, timeout=60)
            if nombre.endswith((".py", ".pyw")):
                compile(datos, nombre, "exec")  # si el archivo vino roto, no se instala nada
            (tmp / nombre).write_bytes(datos)
        if not instalar_requisitos(remota.get("requisitos", [])):
            log("no se pudieron instalar las librerías nuevas; sigo con la versión instalada")
            return False
        respaldo = APP_DIR / "respaldo"
        respaldo.mkdir(exist_ok=True)
        for nombre in remota["archivos"]:
            if (APP_DIR / nombre).exists():
                shutil.copy2(APP_DIR / nombre, respaldo / nombre)
        if LOCAL.exists():
            shutil.copy2(LOCAL, respaldo / "version.json")
        for nombre in remota["archivos"]:
            os.replace(tmp / nombre, APP_DIR / nombre)
        LOCAL.write_text(json.dumps(remota, ensure_ascii=False, indent=1), "utf-8")
        log("actualización instalada")
        return True
    except Exception:
        log("falló la actualización; sigo con la versión instalada\n" + traceback.format_exc())
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def volver_al_respaldo():
    respaldo = APP_DIR / "respaldo"
    if not respaldo.exists():
        return False
    fallida = version_local().get("version")
    for f in respaldo.iterdir():
        shutil.copy2(f, APP_DIR / f.name)
    v = version_local()
    v["saltear"] = fallida
    LOCAL.write_text(json.dumps(v, ensure_ascii=False, indent=1), "utf-8")
    log(f"se volvió a la versión anterior (la {fallida} no arrancó)")
    return True


def con_ventanita(tarea):
    """Muestra un cartelito mientras corre `tarea` (solo si tarda)."""
    import tkinter as tk
    resultado = {}
    raiz = tk.Tk()
    raiz.withdraw()
    raiz.title("Expedientes PJN")
    etiqueta = tk.Label(raiz, text="Buscando actualizaciones…", font=("Segoe UI", 12), padx=30, pady=20)
    etiqueta.pack()

    def aviso(texto):
        raiz.after(0, lambda: (etiqueta.config(text=texto), raiz.deiconify(), raiz.lift()))

    def correr():
        try:
            resultado["valor"] = tarea(aviso)
        finally:
            raiz.after(0, raiz.destroy)

    threading.Thread(target=correr, daemon=True).start()
    raiz.mainloop()
    return resultado.get("valor", False)


def main():
    actualizado = False
    try:
        actualizado = con_ventanita(actualizar)
    except Exception:
        log("error buscando actualizaciones\n" + traceback.format_exc())
    programa = str(APP_DIR / "expedientes_pjn.pyw")
    try:
        runpy.run_path(programa, run_name="__main__")
    except SystemExit:
        pass
    except Exception:
        log("el programa no pudo arrancar\n" + traceback.format_exc())
        if actualizado and volver_al_respaldo():
            sys.modules.pop("pjn_core", None)
            runpy.run_path(programa, run_name="__main__")
        else:
            raise


if __name__ == "__main__":
    main()
