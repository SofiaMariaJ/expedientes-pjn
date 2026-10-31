"""
Expedientes PJN — ventana para bajar los documentos de un expediente del PJN.

1. Ingresar con CUIT/CUIL y clave (se pueden recordar en esta computadora).
2. Pestaña "Mis causas": la lista de Consultas > Mis Expedientes. Elegir una y "Descargar".
3. Pestaña "Consultar cualquier causa": jurisdicción + número + año, "Buscar" y "Descargar".
Al terminar se abre la carpeta con los archivos.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REGISTRO = APP_DIR / "registro.txt"
CONFIG = APP_DIR / "config.json"

# pythonw.exe no tiene consola: mandar la salida a un archivo de registro.
if sys.stdout is None or sys.stderr is None:
    _log = open(REGISTRO, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

sys.path.insert(0, str(APP_DIR))
import pjn_core  # noqa: E402
import mev_core  # noqa: E402
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright  # noqa: E402

try:
    import keyring
except Exception:  # sin keyring no se puede recordar la clave
    keyring = None

SERVICIO = "ExpedientesPJN"
SERVICIO_MEV = "ExpedientesPJN-MEV"
try:
    VERSION = str(json.loads((APP_DIR / "version.json").read_text("utf-8"))["version"])
except Exception:
    VERSION = "7"


# ----------------------------------------------------------------------------- utilidades

def carpeta_documentos():
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            from uuid import UUID

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            u = UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
            g = GUID(u.fields[0], u.fields[1], u.fields[2],
                     (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
            ruta = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ruta)) == 0:
                p = Path(ruta.value)
                ctypes.windll.ole32.CoTaskMemFree(ruta)
                return p
        except Exception:
            pass
    return Path.home() / "Documents"


def abrir_carpeta(p):
    p = str(p)
    if sys.platform == "win32":
        os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def leer_config():
    try:
        return json.loads(CONFIG.read_text("utf-8"))
    except Exception:
        return {}


def guardar_config(cfg):
    try:
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
    except Exception:
        pass


def clave_guardada():
    if keyring is None:
        return None, None
    try:
        usuario = keyring.get_password(SERVICIO, "usuario")
        clave = keyring.get_password(SERVICIO, usuario) if usuario else None
        return usuario, clave
    except Exception:
        return None, None


def recordar_clave(usuario, clave):
    if keyring is None:
        return
    try:
        keyring.set_password(SERVICIO, "usuario", usuario)
        keyring.set_password(SERVICIO, usuario, clave)
    except Exception:
        traceback.print_exc()


def mev_guardada():
    """(usuario, clave, creado_en) de la MEV recordados en esta computadora."""
    if keyring is None:
        return None
    try:
        usuario = keyring.get_password(SERVICIO_MEV, "usuario")
        if not usuario:
            return None
        clave = keyring.get_password(SERVICIO_MEV, usuario)
        creado = keyring.get_password(SERVICIO_MEV, "creado_en") or "aa"
        return (usuario, clave, creado) if clave else None
    except Exception:
        return None


def mev_recordar(usuario, clave, creado):
    if keyring is None:
        return
    try:
        keyring.set_password(SERVICIO_MEV, "usuario", usuario)
        keyring.set_password(SERVICIO_MEV, usuario, clave)
        keyring.set_password(SERVICIO_MEV, "creado_en", creado)
    except Exception:
        traceback.print_exc()


def mev_olvidar():
    if keyring is None:
        return
    try:
        usuario = keyring.get_password(SERVICIO_MEV, "usuario")
        if usuario:
            keyring.delete_password(SERVICIO_MEV, usuario)
        keyring.delete_password(SERVICIO_MEV, "usuario")
        keyring.delete_password(SERVICIO_MEV, "creado_en")
    except Exception:
        pass


def olvidar_clave():
    if keyring is None:
        return
    try:
        usuario = keyring.get_password(SERVICIO, "usuario")
        if usuario:
            keyring.delete_password(SERVICIO, usuario)
        keyring.delete_password(SERVICIO, "usuario")
    except Exception:
        pass


# ----------------------------------------------------------------------------- hilo de trabajo

class Trabajador(threading.Thread):
    """Único hilo que usa Playwright. Recibe órdenes y devuelve eventos a la ventana."""

    def __init__(self, eventos):
        super().__init__(daemon=True)
        self.ordenes = queue.Queue()
        self.eventos = eventos
        self.cancelar = threading.Event()
        self.pjn = None
        self.mev = None
        self.credenciales = None  # solo en memoria, para reingresar si vence la sesión
        self.credenciales_mev = None
        self._pw = None  # un solo motor de navegador para el PJN y la MEV

    def _motor(self):
        if self._pw is None:
            self._pw = sync_playwright().start()
        return self._pw

    def avisar(self, texto, actual=None, total=None):
        print(texto, flush=True)
        self.eventos.put(("progreso", texto, actual, total))

    def _nuevo_pjn(self, visible):
        if self.pjn:
            self.pjn.cerrar()
        self.pjn = pjn_core.PJN(avisar=self.avisar, cancelar=self.cancelar, visible=visible, pw=self._motor())
        self.pjn.abrir_navegador()

    def run(self):
        while True:
            orden, *args = self.ordenes.get()
            if orden == "salir":
                if self.mev:
                    self.mev.cerrar()
                if self.pjn:
                    self.pjn.cerrar()
                try:
                    if self._pw:
                        self._pw.stop()
                except Exception:
                    pass
                return
            self.cancelar.clear()
            try:
                getattr(self, "_" + orden)(*args)
            except pjn_core.Cancelado:
                self.eventos.put(("cancelado",))
            except Exception as e:
                traceback.print_exc()
                self.eventos.put(("error", orden, str(e) or e.__class__.__name__))

    # --- MEV (provincia de Buenos Aires)

    def _sesion_mev(self):
        if self.mev is None:
            if not self.credenciales_mev:
                raise mev_core.SesionVencidaMEV()
            self.mev = mev_core.MEVSesion(self._motor(), avisar=self.avisar, cancelar=self.cancelar)
            self.mev.abrir()
            self.mev.ingresar(*self.credenciales_mev)
        return self.mev

    def _con_mev(self, accion):
        """Ejecuta accion(mev); si la sesión de la MEV venció, vuelve a ingresar y reintenta."""
        for intento in range(3):
            try:
                return accion(self._sesion_mev())
            except mev_core.SesionVencidaMEV:
                if not self.credenciales_mev or intento == 2:
                    raise RuntimeError("La sesión de la MEV venció. Vuelva a ingresar.")
                self.avisar("La sesión de la MEV venció; ingresando de nuevo…")
                if self.mev:
                    self.mev.cerrar()
                self.mev = None

    def _mev_ingresar(self, usuario, clave, creado):
        self.avisar("Ingresando a la MEV…")
        if self.mev:
            self.mev.cerrar()
        self.mev = mev_core.MEVSesion(self._motor(), avisar=self.avisar, cancelar=self.cancelar)
        self.mev.abrir()
        try:
            self.mev.ingresar(usuario, clave, creado)
        except mev_core.ErrorLoginMEV as e:
            self.mev.cerrar()
            self.mev = None
            self.eventos.put(("mev_login_error", str(e)))
            return
        self.credenciales_mev = (usuario, clave, creado)
        self.eventos.put(("mev_login_ok",))

    def _mev_organismos(self, jurisdiccion):
        self.avisar("Buscando los juzgados…")
        organismos = self._con_mev(lambda m: m.elegir_jurisdiccion(jurisdiccion))
        self.eventos.put(("mev_organismos", jurisdiccion, organismos))

    def _mev_buscar(self, jurisdiccion, organismo, modo, texto, estado):
        self.avisar("Buscando en la MEV…")
        res = self._con_mev(lambda m: m.buscar(jurisdiccion, organismo, modo, texto, estado))
        self.eventos.put(("mev_resultados", res))

    def _mev_descargar(self, item, destino):
        nombre = pjn_core.limpiar_nombre(f"{item.get('expediente') or item['nid']} {item.get('caratula', '')}", 90)
        carpeta = Path(destino) / "Provincia (MEV)" / nombre
        carpeta, docs, errores = self._con_mev(lambda m: m.descargar_causa(item["nid"], item["pid"], carpeta))
        self.eventos.put(("descarga_ok", str(carpeta), docs, errores))
        self.eventos.put(("mev_bajada", item))

    def _ingresar(self, usuario, clave):
        self.avisar("Ingresando al PJN…")
        self._nuevo_pjn(visible=False)
        try:
            self.pjn.ingresar(usuario, clave)
            self.credenciales = (usuario, clave)
        except pjn_core.ErrorLogin as e:
            self.eventos.put(("login_error", str(e)))
            return
        except pjn_core.LoginManual:
            self.eventos.put(("progreso", "El PJN pide completar algo más: se abre el navegador…", None, None))
            self._nuevo_pjn(visible=True)
            self.pjn.esperar_ingreso_manual()
        self.eventos.put(("login_ok",))

    def _ingresar_manual(self):
        self.avisar("Abriendo el navegador para ingresar…")
        self._nuevo_pjn(visible=True)
        self.pjn.esperar_ingreso_manual()
        self.eventos.put(("login_ok",))

    def _listar(self, fuera_de_tramite):
        self.pjn.volver_a_invisible()
        self.avisar("Buscando sus expedientes…")
        lista = self.pjn.listar_mis_expedientes(fuera_de_tramite)
        self.eventos.put(("lista", lista))

    def _consultar(self, numero):
        self.avisar(f"Buscando {numero}…")
        for intento in range(2):
            try:
                try:
                    datos = self.pjn.consultar(numero)
                except pjn_core.NecesitaVerificacion:
                    self.eventos.put(("verificacion",))
                    datos = self.pjn.consultar_con_verificacion(numero)
                break
            except pjn_core.SesionVencida:
                if not self.credenciales or intento == 1:
                    raise
                self.avisar("La sesión venció; ingresando de nuevo…")
                self.pjn.ingresar(*self.credenciales)
        self.eventos.put(("consulta", numero, datos))

    def _descargar(self, numero, destino, vinculados, solo_consulta=False):
        # Si el PJN se cuelga o vence la sesión, se reintenta: lo ya bajado no se repite.
        intentos = 4
        if solo_consulta and getattr(self.pjn, "_oculto", None):
            self.avisar("No cierre la ventana del PJN mientras se descarga.")
        try:
            self._descargar_con_reintentos(numero, destino, vinculados, solo_consulta, intentos)
        finally:
            if solo_consulta:
                self.pjn.volver_a_invisible()

    def _descargar_con_reintentos(self, numero, destino, vinculados, solo_consulta, intentos):
        for intento in range(1, intentos + 1):
            try:
                carpeta, docs, errores = self.pjn.descargar_expediente(
                    numero, destino, vinculados, solo_consulta)
                break
            except pjn_core.NecesitaVerificacion:
                if intento == intentos:
                    raise RuntimeError("El PJN pide el verificador y no se completó.")
                self.eventos.put(("verificacion",))
                self.pjn.consultar_con_verificacion(numero)
            except pjn_core.SesionVencida:
                if not self.credenciales or intento == intentos:
                    raise
                self.avisar("La sesión venció; ingresando de nuevo y sigo desde donde quedó…")
                self.pjn.ingresar(*self.credenciales)
            except (pjn_core.PaginaNoCargo, PWTimeout) as e:
                if intento == intentos:
                    raise RuntimeError(
                        f"El PJN dejó de responder ({e}). Lo que se bajó quedó guardado: "
                        "vuelva a descargar la misma causa más tarde y sigue desde ahí.")
                self.avisar(f"El PJN no responde; espero y sigo desde donde quedó (intento {intento + 1} de {intentos})…")
                time.sleep(20 * intento)
        self.eventos.put(("descarga_ok", str(carpeta), docs, errores))


# ----------------------------------------------------------------------------- ventana

FAMILIA = "Segoe UI" if sys.platform == "win32" else "Helvetica Neue"

# Colores (tono índigo del ícono)
C_FONDO = "#F3F4F9"
C_TARJETA = "#FFFFFF"
C_BORDE = "#E2E4EE"
C_TEXTO = "#1B1F33"
C_SUAVE = "#5F6580"
C_ACENTO = "#3F51B5"
C_ACENTO_HOVER = "#32418F"
C_ACENTO_CLARO = "#E8EBF8"
C_OK_FONDO = "#E7F4EA"
C_OK_TEXTO = "#1E6B2E"
C_ERROR = "#B3261E"
C_ERROR_FONDO = "#FBEAE8"


def fuente(tam=14, peso="normal"):
    return ctk.CTkFont(family=FAMILIA, size=tam, weight=peso)


def boton(padre, texto, comando, principal=True, **kw):
    if principal:
        estilo = dict(fg_color=C_ACENTO, hover_color=C_ACENTO_HOVER, text_color="#FFFFFF",
                      text_color_disabled="#C9CEEA", font=fuente(15, "bold"), height=44, corner_radius=10)
    else:
        estilo = dict(fg_color=C_TARJETA, hover_color=C_ACENTO_CLARO, text_color=C_ACENTO,
                      text_color_disabled="#A9AEC6", border_width=1, border_color=C_BORDE,
                      font=fuente(13), height=36, corner_radius=8)
    estilo.update(kw)
    return ctk.CTkButton(padre, text=texto, command=comando, **estilo)


def tarjeta(padre, **kw):
    estilo = dict(fg_color=C_TARJETA, corner_radius=14, border_width=1, border_color=C_BORDE)
    estilo.update(kw)
    return ctk.CTkFrame(padre, **estilo)


def campo(padre, placeholder, ancho=320, **kw):
    return ctk.CTkEntry(padre, placeholder_text=placeholder, width=ancho, height=42, corner_radius=8,
                        border_width=1, border_color=C_BORDE, fg_color="#FBFBFE", text_color=C_TEXTO,
                        font=fuente(14), **kw)


def logo(padre, tam=40):
    return ctk.CTkLabel(padre, text="PJN", width=tam, height=tam, corner_radius=10, fg_color=C_ACENTO,
                        text_color="#FFFFFF", font=fuente(int(tam * 0.34), "bold"))


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = leer_config()
        self.eventos = queue.Queue()
        self.trabajador = Trabajador(self.eventos)
        self.trabajador.start()
        self.lista = []
        self.ocupado = False
        self.ultima_carpeta = None
        self._credenciales = None
        self.usuario_actual = ""
        self.causa_encontrada = None
        self.btn_ingresar = None
        self.btn_descargar = None
        self.mev_conectada = False
        self.pjn_conectado = False

        root.title(f"Expedientes PJN — versión {VERSION}")
        root.configure(fg_color=C_FONDO)
        root.geometry("1180x760")
        root.minsize(900, 600)
        if sys.platform == "win32":
            root.after(50, lambda: root.state("zoomed"))  # maximizada
        if sys.platform == "win32":  # customtkinter pone su ícono a los 200 ms: el nuestro va después
            root.after(300, lambda: self._poner_icono())
        self._estilo_tabla()

        self.marco = ctk.CTkFrame(root, fg_color="transparent")
        self.marco.pack(fill="both", expand=True)
        self.pantalla_inicio()
        root.protocol("WM_DELETE_WINDOW", self.salir)
        root.after(100, self.procesar_eventos)

    def _poner_icono(self):
        try:
            self.root.iconbitmap(str(APP_DIR / "icono.ico"))
        except Exception:
            pass

    def _estilo_tabla(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        f = tkfont.Font(family=FAMILIA, size=11)
        fb = tkfont.Font(family=FAMILIA, size=11, weight="bold")
        alto = f.metrics("linespace") + 18
        st.configure("PJN.Treeview", background=C_TARJETA, fieldbackground=C_TARJETA, foreground=C_TEXTO,
                     rowheight=alto, font=f, borderwidth=0, relief="flat")
        st.configure("PJN.Treeview.Heading", background=C_FONDO, foreground=C_SUAVE, font=fb,
                     relief="flat", borderwidth=0, padding=(10, 8))
        st.map("PJN.Treeview.Heading", background=[("active", C_ACENTO_CLARO)])
        st.map("PJN.Treeview", background=[("selected", C_ACENTO_CLARO)],
               foreground=[("selected", C_TEXTO)])
        st.layout("PJN.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # --- pantallas

    def limpiar(self):
        for w in self.marco.winfo_children():
            w.destroy()
        self.btn_ingresar = None
        self.btn_descargar = None

    def pantalla_inicio(self):
        """Primera pantalla: elegir si entra a las causas de Nación (PJN) o de Provincia (MEV)."""
        self.limpiar()
        caja = tarjeta(self.marco, corner_radius=18)
        caja.place(relx=0.5, rely=0.47, anchor="center")
        interior = ctk.CTkFrame(caja, fg_color="transparent")
        interior.pack(padx=48, pady=40)
        logo(interior, 56).pack()
        ctk.CTkLabel(interior, text="Expedientes PJN", font=fuente(26, "bold"),
                     text_color=C_TEXTO).pack(pady=(16, 2))
        ctk.CTkLabel(interior, text="¿Qué causas quiere ver?", font=fuente(15),
                     text_color=C_SUAVE).pack(pady=(0, 26))
        opciones = ctk.CTkFrame(interior, fg_color="transparent")
        opciones.pack()
        for col, (titulo, detalle, accion) in enumerate([
                ("Nación", "Poder Judicial de la Nación (PJN)\nSe ingresa con el CUIT", self.elegir_pjn),
                ("Provincia de Buenos Aires", "Mesa de Entradas Virtual (MEV)\nSe ingresa con el usuario de la MEV",
                 self.elegir_mev)]):
            op = ctk.CTkFrame(opciones, fg_color=C_ACENTO_CLARO, corner_radius=14, border_width=1,
                              border_color="#C9CFEE")
            op.grid(row=0, column=col, padx=10)
            ctk.CTkLabel(op, text=titulo, font=fuente(19, "bold"), text_color=C_TEXTO).pack(padx=26, pady=(22, 4))
            ctk.CTkLabel(op, text=detalle, font=fuente(13), text_color=C_SUAVE, justify="center").pack(padx=26)
            boton(op, "Entrar", accion, width=220).pack(padx=26, pady=(16, 22))
        ctk.CTkLabel(interior, text="Una vez adentro puede pasar de una a otra con las pestañas de arriba.",
                     font=fuente(13), text_color=C_SUAVE).pack(pady=(22, 0))

    def elegir_pjn(self):
        if self.ocupado:
            messagebox.showinfo("Expedientes PJN", "Espere a que termine lo que se está haciendo.")
            return
        self.pantalla_ingreso()
        usuario, clave = clave_guardada()
        if usuario and clave:  # entra sola con los datos recordados
            self.e_usuario.insert(0, usuario)
            self.e_clave.insert(0, clave)
            self.var_recordar.set(True)
            self.root.after(400, self.ingresar)

    def elegir_mev(self):
        self.pantalla_principal("Provincia (MEV)")

    def volver_desde_ingreso(self):
        if self.ocupado:
            return
        if self.mev_conectada:
            self.pantalla_principal("Provincia (MEV)")
        else:
            self.pantalla_inicio()

    def pantalla_ingreso(self):
        self.limpiar()
        caja = tarjeta(self.marco, corner_radius=18)
        caja.place(relx=0.5, rely=0.47, anchor="center")
        interior = ctk.CTkFrame(caja, fg_color="transparent")
        interior.pack(padx=44, pady=40)

        logo(interior, 56).pack()
        ctk.CTkLabel(interior, text="Expedientes PJN", font=fuente(26, "bold"),
                     text_color=C_TEXTO).pack(pady=(16, 2))
        ctk.CTkLabel(interior, text="Ingrese con su usuario del Poder Judicial de la Nación",
                     font=fuente(14), text_color=C_SUAVE).pack(pady=(0, 24))

        ctk.CTkLabel(interior, text="CUIT / CUIL", font=fuente(13, "bold"), text_color=C_TEXTO,
                     anchor="w").pack(fill="x")
        self.e_usuario = campo(interior, "Solo números, por ejemplo 20123456789", ancho=360)
        self.e_usuario.pack(pady=(4, 14))
        ctk.CTkLabel(interior, text="Clave", font=fuente(13, "bold"), text_color=C_TEXTO,
                     anchor="w").pack(fill="x")
        self.e_clave = campo(interior, "Su clave del PJN", ancho=360, show="•")
        self.e_clave.pack(pady=(4, 14))
        self.var_recordar = getattr(self, "var_recordar", tk.BooleanVar(value=False))
        ctk.CTkCheckBox(interior, text="Recordar mis datos en esta computadora", variable=self.var_recordar,
                        font=fuente(13), text_color=C_TEXTO, fg_color=C_ACENTO, hover_color=C_ACENTO_HOVER,
                        border_color="#B9BED6", checkbox_width=20, checkbox_height=20,
                        corner_radius=5).pack(anchor="w", pady=(0, 20))
        self.btn_ingresar = boton(interior, "Ingresar", self.ingresar, width=360)
        self.btn_ingresar.pack()
        self.lbl_estado = ctk.CTkLabel(interior, text="", font=fuente(13), text_color=C_SUAVE,
                                       wraplength=360, justify="center")
        self.lbl_estado.pack(pady=(14, 0))
        ctk.CTkButton(interior, text="Ingresar desde el navegador", command=self.ingresar_manual,
                      fg_color="transparent", hover_color=C_ACENTO_CLARO, text_color=C_ACENTO,
                      font=fuente(13), height=30).pack(pady=(6, 0))
        ctk.CTkButton(interior, text="Volver", command=self.volver_desde_ingreso,
                      fg_color="transparent", hover_color=C_ACENTO_CLARO, text_color=C_SUAVE,
                      font=fuente(13), height=30).pack(pady=(2, 0))

        self.e_usuario.bind("<Return>", lambda e: self.e_clave.focus_set())
        self.e_clave.bind("<Return>", lambda e: self.ingresar())
        self.root.after(100, self._enfocar_ingreso)

    def _enfocar_ingreso(self):
        try:
            if self.e_usuario.winfo_exists():
                (self.e_clave if self.e_usuario.get() else self.e_usuario).focus_set()
        except tk.TclError:
            pass

    def pantalla_principal(self, seccion="Mis causas"):
        self.limpiar()
        m = self.marco

        # Barra superior
        barra = ctk.CTkFrame(m, fg_color=C_TARJETA, corner_radius=0, height=68)
        barra.pack(fill="x")
        barra.pack_propagate(False)
        logo(barra, 38).pack(side="left", padx=(24, 12))
        ctk.CTkLabel(barra, text="Expedientes PJN", font=fuente(19, "bold"),
                     text_color=C_TEXTO).pack(side="left")
        if self.pjn_conectado:
            boton(barra, "Salir del PJN", self.cerrar_sesion, principal=False).pack(side="right", padx=24)
        else:
            boton(barra, "Inicio", self.ir_al_inicio, principal=False).pack(side="right", padx=24)
        if self.pjn_conectado and self.usuario_actual:
            ctk.CTkLabel(barra, text=f"CUIT {self.usuario_actual}", font=fuente(13),
                         text_color=C_SUAVE).pack(side="right")
        ctk.CTkFrame(m, fg_color=C_BORDE, height=1, corner_radius=0).pack(fill="x")
        self._armar_pie(m)  # va primero y abajo: siempre queda visible entero

        cuerpo = ctk.CTkFrame(m, fg_color="transparent")
        cuerpo.pack(fill="both", expand=True, padx=24, pady=(18, 0))

        self.selector = ctk.CTkSegmentedButton(
            cuerpo, values=["Mis causas", "Consultar cualquier causa", "Provincia (MEV)"],
            command=self._cambiar_seccion,
            font=fuente(14, "bold"), height=40, corner_radius=10, fg_color="#E4E6F0",
            selected_color=C_ACENTO, selected_hover_color=C_ACENTO_HOVER,
            unselected_color="#E4E6F0", unselected_hover_color="#D8DBEA",
            text_color=("#FFFFFF", "#FFFFFF"))
        self.selector.pack(anchor="w")
        self._ajustar_texto_selector()

        self.contenedor = ctk.CTkFrame(cuerpo, fg_color="transparent")
        self.contenedor.pack(fill="both", expand=True, pady=(14, 0))
        self.sec_mias = self._armar_mias(self.contenedor)
        self.sec_otra = self._armar_consulta(self.contenedor)
        self.sec_mev = self._armar_mev(self.contenedor)
        self.sec_sin_pjn = self._armar_sin_pjn(self.contenedor)
        if self.mev_conectada:  # ya estaba adentro de la MEV (por ejemplo, volvió de ingresar al PJN)
            self._mev_mostrar_busqueda()

        self.selector.set(seccion)
        self._cambiar_seccion(seccion)

    def _ajustar_texto_selector(self):
        # El texto de la opción no elegida va en gris oscuro (la elegida, en blanco).
        try:
            for nombre, b in self.selector._buttons_dict.items():
                elegido = nombre == self.selector.get()
                b.configure(text_color="#FFFFFF" if elegido else C_TEXTO)
        except Exception:
            pass

    def _cambiar_seccion(self, valor):
        for sec in (self.sec_mias, self.sec_otra, self.sec_mev, self.sec_sin_pjn):
            sec.pack_forget()
        if valor != "Provincia (MEV)" and not self.pjn_conectado:
            self.sec_sin_pjn.pack(fill="both", expand=True)
        else:
            {"Mis causas": self.sec_mias, "Consultar cualquier causa": self.sec_otra}.get(
                valor, self.sec_mev).pack(fill="both", expand=True)
        self._ajustar_texto_selector()
        if valor == "Provincia (MEV)" and not self.mev_conectada and not self.ocupado:
            guardada = mev_guardada()
            if guardada:  # entra sola con los datos recordados
                self.e_mev_usuario.delete(0, "end")
                self.e_mev_usuario.insert(0, guardada[0])
                self.e_mev_clave.delete(0, "end")
                self.e_mev_clave.insert(0, guardada[1])
                self.var_mev_creado.set(self._nombre_creado(guardada[2]))
                self.var_mev_recordar.set(True)
                self.mev_ingresar()

    def _armar_sin_pjn(self, padre):
        sec = tarjeta(padre)
        centro = ctk.CTkFrame(sec, fg_color="transparent")
        centro.place(relx=0.5, rely=0.42, anchor="center")
        ctk.CTkLabel(centro, text="Causas de Nación", font=fuente(20, "bold"), text_color=C_TEXTO).pack()
        ctk.CTkLabel(centro, text="Para ver sus causas del Poder Judicial de la Nación\n"
                                  "tiene que ingresar al PJN con su CUIT.",
                     font=fuente(14), text_color=C_SUAVE, justify="center").pack(pady=(6, 18))
        boton(centro, "Ingresar al PJN", self.elegir_pjn, width=240).pack()
        return sec

    def ir_al_inicio(self):
        if self.ocupado:
            return
        self.pantalla_inicio()

    def _armar_mias(self, padre):
        sec = tarjeta(padre)
        arriba = ctk.CTkFrame(sec, fg_color="transparent")
        arriba.pack(fill="x", padx=18, pady=(16, 10))
        self.e_buscar = campo(arriba, "Buscar por número, carátula o juzgado…", ancho=380)
        self.e_buscar.pack(side="left")
        self.e_buscar.bind("<KeyRelease>", lambda e: self.mostrar_lista())
        self.var_fuera = tk.BooleanVar(value=self.cfg.get("fuera_de_tramite", False))
        ctk.CTkSwitch(arriba, text="Incluir los que están fuera de trámite", variable=self.var_fuera,
                      command=self.cambio_fuera, font=fuente(13), text_color=C_TEXTO,
                      progress_color=C_ACENTO).pack(side="left", padx=18)
        boton(arriba, "Actualizar lista", self.listar, principal=False, width=130).pack(side="right")
        self.lbl_cuenta = ctk.CTkLabel(arriba, text="", font=fuente(13), text_color=C_SUAVE)
        self.lbl_cuenta.pack(side="right", padx=12)

        tabla = ctk.CTkFrame(sec, fg_color=C_TARJETA, corner_radius=0)
        tabla.pack(fill="both", expand=True, padx=18)
        cols = ("expediente", "caratula", "dependencia", "situacion", "ult_act")
        self.tree = ttk.Treeview(tabla, columns=cols, show="headings", selectmode="browse",
                                 style="PJN.Treeview")
        for c, titulo, ancho in [("expediente", "Expediente", 160), ("caratula", "Carátula", 430),
                                 ("dependencia", "Juzgado", 250), ("situacion", "Situación", 120),
                                 ("ult_act", "Últ. actuación", 110)]:
            self.tree.heading(c, text=titulo, anchor="w")
            self.tree.column(c, width=ancho, anchor="w", stretch=(c == "caratula"))
        self.tree.tag_configure("par", background="#F8F9FD")
        sb = ctk.CTkScrollbar(tabla, command=self.tree.yview, button_color="#C9CDE0",
                              button_hover_color="#AEB3CC")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.descargar())

        abajo = ctk.CTkFrame(sec, fg_color="transparent")
        abajo.pack(fill="x", padx=18, pady=14)
        self.btn_descargar = boton(abajo, "Descargar la causa elegida", self.descargar, width=300)
        self.btn_descargar.pack(side="left")
        ctk.CTkLabel(abajo, text="Haga clic en una causa de la lista (o doble clic para descargarla).",
                     font=fuente(13), text_color=C_SUAVE).pack(side="left", padx=16)
        return sec

    def _armar_consulta(self, padre):
        sec = tarjeta(padre)
        # Se puede desplazar: con letra grande o ventana chica no queda nada tapado.
        f = self.consulta_scroll = ctk.CTkScrollableFrame(sec, fg_color="transparent",
                                                          scrollbar_button_color="#C9CDE0",
                                                          scrollbar_button_hover_color="#AEB3CC")
        f.pack(fill="both", expand=True, padx=(24, 8), pady=10)
        ctk.CTkLabel(f, text="Consultar cualquier causa", font=fuente(20, "bold"),
                     text_color=C_TEXTO, anchor="w").pack(fill="x")
        ctk.CTkLabel(f, text="Sirve para causas que no son suyas. Elija la jurisdicción, escriba número y año, "
                             "y apriete «Buscar».", font=fuente(14), text_color=C_SUAVE,
                     anchor="w").pack(fill="x", pady=(2, 14))

        ctk.CTkLabel(f, text="Jurisdicción", font=fuente(13, "bold"), text_color=C_TEXTO,
                     anchor="w").pack(fill="x")
        opciones = [f"{sig} - {nom}" for sig, nom in pjn_core.JURISDICCIONES]
        guardada = self.cfg.get("ultima_jurisdiccion")
        self.var_jur = tk.StringVar(value=guardada if guardada in opciones else opciones[10])
        ctk.CTkOptionMenu(f, values=opciones, variable=self.var_jur, width=480, height=42, corner_radius=8,
                          fg_color="#EEF0F8", button_color="#E1E4F2", button_hover_color="#D3D7EB",
                          text_color=C_TEXTO, font=fuente(14), dropdown_font=fuente(13),
                          dropdown_fg_color=C_TARJETA, dropdown_hover_color=C_ACENTO_CLARO,
                          dropdown_text_color=C_TEXTO, dynamic_resizing=False).pack(anchor="w", pady=(4, 16))

        fila = ctk.CTkFrame(f, fg_color="transparent")
        fila.pack(anchor="w")
        col1 = ctk.CTkFrame(fila, fg_color="transparent")
        col1.pack(side="left")
        ctk.CTkLabel(col1, text="Número", font=fuente(13, "bold"), text_color=C_TEXTO,
                     anchor="w").pack(fill="x")
        self.e_num = campo(col1, "Ej. 5000", ancho=170)
        self.e_num.pack(pady=(4, 0))
        col2 = ctk.CTkFrame(fila, fg_color="transparent")
        col2.pack(side="left", padx=(16, 0))
        ctk.CTkLabel(col2, text="Año", font=fuente(13, "bold"), text_color=C_TEXTO,
                     anchor="w").pack(fill="x")
        self.e_anio = campo(col2, "Ej. 2019", ancho=110)
        self.e_anio.pack(pady=(4, 0))
        self.btn_buscar = boton(fila, "Buscar", self.buscar_causa, width=140)
        self.btn_buscar.pack(side="left", padx=(16, 0), anchor="s")
        self.e_num.bind("<Return>", lambda e: self.e_anio.focus_set())
        self.e_anio.bind("<Return>", lambda e: self.buscar_causa())

        # Resultado: datos a la izquierda y el botón a la derecha (así nunca queda tapado).
        self.res = ctk.CTkFrame(f, fg_color=C_OK_FONDO, corner_radius=12)
        self.res.grid_columnconfigure(0, weight=1)
        info = ctk.CTkFrame(self.res, fg_color="transparent")
        info.grid(row=0, column=0, sticky="nsew", padx=(18, 8), pady=12)
        self.lbl_res_titulo = ctk.CTkLabel(info, text="", font=fuente(15, "bold"), anchor="w",
                                           justify="left", text_color=C_OK_TEXTO)
        self.lbl_res_titulo.pack(fill="x")
        self.lbl_res_caratula = ctk.CTkLabel(info, text="", font=fuente(14), anchor="w", justify="left",
                                             text_color=C_TEXTO, wraplength=620)
        self.lbl_res_caratula.pack(fill="x")
        self.lbl_res_detalle = ctk.CTkLabel(info, text="", font=fuente(13), anchor="w", justify="left",
                                            text_color=C_SUAVE, wraplength=620)
        self.lbl_res_detalle.pack(fill="x")

        def ajustar_ancho(e):  # el texto largo baja de renglón en vez de cortarse
            ancho = max(200, int(e.width / ctk.ScalingTracker.get_widget_scaling(info)) - 10)
            self.lbl_res_caratula.configure(wraplength=ancho)
            self.lbl_res_detalle.configure(wraplength=ancho)
        info.bind("<Configure>", ajustar_ancho)
        self.btn_descargar_otra = boton(self.res, "Descargar esta causa", self.descargar_otra, width=230)
        self.btn_descargar_otra.grid(row=0, column=1, sticky="e", padx=18, pady=12)
        return sec

    # --- sección Provincia (MEV)

    def _nombre_creado(self, codigo):
        return next((n for c, n in mev_core.CREADO_EN if c == codigo), mev_core.CREADO_EN[0][1])

    def _armar_mev(self, padre):
        sec = tarjeta(padre)
        f = self.mev_scroll = ctk.CTkScrollableFrame(sec, fg_color="transparent", scrollbar_button_color="#C9CDE0",
                                                     scrollbar_button_hover_color="#AEB3CC")
        f.pack(fill="both", expand=True, padx=(24, 8), pady=10)
        ctk.CTkLabel(f, text="Provincia de Buenos Aires (MEV)", font=fuente(20, "bold"), text_color=C_TEXTO,
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(f, text="Causas de la Mesa de Entradas Virtual de la Suprema Corte bonaerense.",
                     font=fuente(14), text_color=C_SUAVE, anchor="w").pack(fill="x", pady=(2, 14))
        estilo_menu = dict(height=42, corner_radius=8, fg_color="#EEF0F8", button_color="#E1E4F2",
                           button_hover_color="#D3D7EB", text_color=C_TEXTO, font=fuente(14), dropdown_font=fuente(13),
                           dropdown_fg_color=C_TARJETA, dropdown_hover_color=C_ACENTO_CLARO,
                           dropdown_text_color=C_TEXTO, dynamic_resizing=False)

        # Ingreso a la MEV (usuario propio de la MEV, no es el CUIT)
        self.mev_login = ctk.CTkFrame(f, fg_color="transparent")
        self.mev_login.pack(fill="x")
        fila = ctk.CTkFrame(self.mev_login, fg_color="transparent")
        fila.pack(anchor="w")
        for col, (titulo, attr, ph, extra) in enumerate([("Usuario de la MEV", "e_mev_usuario", "No es el CUIT", {}),
                                                         ("Clave", "e_mev_clave", "Clave de la MEV", {"show": "•"})]):
            c = ctk.CTkFrame(fila, fg_color="transparent")
            c.grid(row=0, column=col, padx=(0, 14), sticky="w")
            ctk.CTkLabel(c, text=titulo, font=fuente(13, "bold"), text_color=C_TEXTO, anchor="w").pack(fill="x")
            e = campo(c, ph, ancho=230, **extra)
            e.pack(pady=(4, 0))
            setattr(self, attr, e)
        c = ctk.CTkFrame(fila, fg_color="transparent")
        c.grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(c, text="Creado en", font=fuente(13, "bold"), text_color=C_TEXTO, anchor="w").pack(fill="x")
        self.var_mev_creado = tk.StringVar(value=self._nombre_creado(self.cfg.get("mev_creado_en", "aa")))
        ctk.CTkOptionMenu(c, values=[n for _, n in mev_core.CREADO_EN], variable=self.var_mev_creado, width=260,
                          **estilo_menu).pack(pady=(4, 0))
        self.var_mev_recordar = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.mev_login, text="Recordar mis datos de la MEV en esta computadora",
                        variable=self.var_mev_recordar, font=fuente(13), text_color=C_TEXTO, fg_color=C_ACENTO,
                        hover_color=C_ACENTO_HOVER, border_color="#B9BED6", checkbox_width=18, checkbox_height=18,
                        corner_radius=4).pack(anchor="w", pady=(12, 10))
        self.btn_mev_ingresar = boton(self.mev_login, "Ingresar a la MEV", self.mev_ingresar, width=220)
        self.btn_mev_ingresar.pack(anchor="w")
        self.e_mev_clave.bind("<Return>", lambda e: self.mev_ingresar())

        # Búsqueda (aparece después de ingresar)
        self.mev_busqueda = ctk.CTkFrame(f, fg_color="transparent")
        fila = ctk.CTkFrame(self.mev_busqueda, fg_color="transparent")
        fila.pack(anchor="w", fill="x")
        c = ctk.CTkFrame(fila, fg_color="transparent")
        c.grid(row=0, column=0, padx=(0, 14), sticky="w")
        ctk.CTkLabel(c, text="Departamento judicial", font=fuente(13, "bold"), text_color=C_TEXTO,
                     anchor="w").pack(fill="x")
        nombres_jur = [n for _, n in mev_core.JURISDICCIONES]
        self.var_mev_jur = tk.StringVar(value=self.cfg.get("mev_jur") if self.cfg.get("mev_jur") in nombres_jur
                                        else "Elija…")
        ctk.CTkOptionMenu(c, values=nombres_jur, variable=self.var_mev_jur, width=280,
                          command=lambda v: self.mev_cargar_organismos(), **estilo_menu).pack(pady=(4, 0))
        c = ctk.CTkFrame(fila, fg_color="transparent")
        c.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(c, text="Juzgado / organismo", font=fuente(13, "bold"), text_color=C_TEXTO,
                     anchor="w").pack(fill="x")
        self.var_mev_org = tk.StringVar(value="Primero elija el departamento")
        self.menu_mev_org = ctk.CTkOptionMenu(c, values=["Primero elija el departamento"], variable=self.var_mev_org,
                                              width=420, **estilo_menu)
        self.menu_mev_org.pack(pady=(4, 0))
        self.mev_organismos = []

        fila = ctk.CTkFrame(self.mev_busqueda, fg_color="transparent")
        fila.pack(anchor="w", pady=(14, 0))
        self.var_mev_modo = tk.StringVar(value="Número")
        ctk.CTkSegmentedButton(fila, values=["Número", "Carátula", "Receptoría"], variable=self.var_mev_modo,
                               font=fuente(13, "bold"), height=40, corner_radius=8, fg_color="#E4E6F0",
                               selected_color=C_ACENTO, selected_hover_color=C_ACENTO_HOVER,
                               unselected_color="#D8DBEA", unselected_hover_color="#CCD0E3",
                               text_color="#FFFFFF").grid(row=0, column=0, padx=(0, 10))
        self.e_mev_texto = campo(fila, "Número de expediente o parte de la carátula", ancho=330)
        self.e_mev_texto.grid(row=0, column=1, padx=(0, 10))
        self.e_mev_texto.bind("<Return>", lambda e: self.mev_buscar())
        fila = ctk.CTkFrame(self.mev_busqueda, fg_color="transparent")
        fila.pack(anchor="w", pady=(10, 0))
        self.var_mev_estado = tk.StringVar(value="Activas y archivadas")
        ctk.CTkOptionMenu(fila, values=["Activas y archivadas", "Solo activas", "Solo archivadas"],
                          variable=self.var_mev_estado, width=220, **estilo_menu).grid(row=0, column=0, padx=(0, 10))
        self.btn_mev_buscar = boton(fila, "Buscar", self.mev_buscar, width=140)
        self.btn_mev_buscar.grid(row=0, column=1)

        self.lbl_mev_lista = ctk.CTkLabel(self.mev_busqueda, text="", font=fuente(14, "bold"), text_color=C_TEXTO,
                                          anchor="w")
        self.lbl_mev_lista.pack(fill="x", pady=(16, 6))
        tabla = ctk.CTkFrame(self.mev_busqueda, fg_color=C_TARJETA, corner_radius=0)
        tabla.pack(fill="x")
        cols = ("expediente", "caratula", "estado", "inicio", "ultimo")
        self.tree_mev = ttk.Treeview(tabla, columns=cols, show="headings", selectmode="browse",
                                     style="PJN.Treeview", height=7)
        for col, titulo, ancho in [("expediente", "Expediente", 150), ("caratula", "Carátula", 430),
                                   ("estado", "Estado", 110), ("inicio", "Inicio", 95), ("ultimo", "Último paso", 220)]:
            self.tree_mev.heading(col, text=titulo, anchor="w")
            self.tree_mev.column(col, width=ancho, anchor="w", stretch=(col == "caratula"))
        self.tree_mev.tag_configure("par", background="#F8F9FD")
        sb = ctk.CTkScrollbar(tabla, command=self.tree_mev.yview, button_color="#C9CDE0",
                              button_hover_color="#AEB3CC")
        self.tree_mev.configure(yscrollcommand=sb.set)
        self.tree_mev.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        self.tree_mev.bind("<Double-1>", lambda e: self.mev_descargar())
        abajo = ctk.CTkFrame(self.mev_busqueda, fg_color="transparent")
        abajo.pack(fill="x", pady=(12, 4))
        self.btn_mev_descargar = boton(abajo, "Descargar la causa elegida", self.mev_descargar, width=280)
        self.btn_mev_descargar.pack(side="left")
        ctk.CTkButton(abajo, text="Salir de la MEV", command=self.mev_salir, fg_color="transparent",
                      hover_color=C_ACENTO_CLARO, text_color=C_ACENTO, font=fuente(13), width=120,
                      height=30).pack(side="right")
        self.mev_items = []
        return sec

    def _mostrar_mev_items(self, items, titulo):
        self.mev_items = items
        self.tree_mev.delete(*self.tree_mev.get_children())
        for i, it in enumerate(items):
            self.tree_mev.insert("", "end", iid=str(i), tags=("par",) if i % 2 else (), values=(
                it.get("expediente", ""), it.get("caratula", ""), it.get("estado", ""), it.get("inicio", ""),
                it.get("ultimo", "")))
        self.lbl_mev_lista.configure(text=titulo)

    def _mostrar_recientes_mev(self):
        recientes = self.cfg.get("mev_recientes", [])
        self._mostrar_mev_items(recientes, "Causas de provincia bajadas antes (para actualizarlas)" if recientes
                                else "Busque una causa para descargarla.")

    def mev_ingresar(self):
        if self.ocupado:
            return
        usuario = self.e_mev_usuario.get().strip()
        clave = self.e_mev_clave.get()
        creado = next((c for c, n in mev_core.CREADO_EN if n == self.var_mev_creado.get()), "aa")
        if not usuario or not clave:
            self.estado("Escriba el usuario y la clave de la MEV.", error=True)
            return
        self._mev_cred = (usuario, clave, creado)
        self.set_ocupado(True)
        self.trabajador.ordenes.put(("mev_ingresar", usuario, clave, creado))

    def mev_salir(self):
        if self.ocupado:
            return
        if self.var_mev_recordar.get() and messagebox.askyesno(
                "Salir de la MEV", "¿Olvidar también el usuario y la clave de la MEV guardados en esta computadora?"):
            mev_olvidar()
            self.var_mev_recordar.set(False)
        self.trabajador.credenciales_mev = None
        self.mev_conectada = False
        self.mev_busqueda.pack_forget()
        self.mev_login.pack(fill="x")

    def mev_cargar_organismos(self):
        codigo = next((c for c, n in mev_core.JURISDICCIONES if n == self.var_mev_jur.get()), None)
        if not codigo or self.ocupado:
            return
        self.cfg["mev_jur"] = self.var_mev_jur.get()
        guardar_config(self.cfg)
        self.menu_mev_org.configure(values=["Cargando…"])
        self.var_mev_org.set("Cargando…")
        self.set_ocupado(True)
        self.trabajador.ordenes.put(("mev_organismos", codigo))

    def mev_buscar(self):
        if self.ocupado:
            return
        jur = next((c for c, n in mev_core.JURISDICCIONES if n == self.var_mev_jur.get()), None)
        org = next((c for c, n in self.mev_organismos if n == self.var_mev_org.get()), None)
        texto = self.e_mev_texto.get().strip()
        if not jur or not org:
            messagebox.showinfo("Expedientes PJN", "Elija el departamento judicial y el juzgado.")
            return
        if not texto:
            messagebox.showinfo("Expedientes PJN", "Escriba el número de expediente o parte de la carátula.")
            return
        modo = {"Número": "numero", "Carátula": "caratula", "Receptoría": "receptoria"}[self.var_mev_modo.get()]
        estado = {"Activas y archivadas": "ambas", "Solo activas": "activas",
                  "Solo archivadas": "archivadas"}[self.var_mev_estado.get()]
        self.cfg["mev_org"] = self.var_mev_org.get()
        guardar_config(self.cfg)
        self.set_ocupado(True)
        self.trabajador.ordenes.put(("mev_buscar", jur, org, modo, texto, estado))

    def mev_descargar(self):
        if self.ocupado:
            return
        sel = self.tree_mev.selection()
        if not sel:
            messagebox.showinfo("Expedientes PJN", "Primero haga clic en una causa de la lista.")
            return
        it = self.mev_items[int(sel[0])]
        if not messagebox.askyesno("Descargar", "¿Descargar todos los pasos procesales y adjuntos de\n\n"
                                                f"{it.get('expediente', '')}\n{it.get('caratula', '')}?"):
            return
        destino = self.var_destino.get()
        Path(destino).mkdir(parents=True, exist_ok=True)
        self.barra.set(0)
        self.set_ocupado(True)
        self.btn_abrir.configure(state="disabled")
        self.trabajador.ordenes.put(("mev_descargar", it, destino))

    def _armar_pie(self, m):
        pie = ctk.CTkFrame(m, fg_color="transparent")
        pie.pack(side="bottom", fill="x", padx=24, pady=(12, 18))
        t = tarjeta(pie)
        t.pack(fill="x")
        fila1 = ctk.CTkFrame(t, fg_color="transparent")
        fila1.pack(fill="x", padx=18, pady=(14, 6))
        self.lbl_estado = ctk.CTkLabel(fila1, text="", font=fuente(13), text_color=C_TEXTO, anchor="w",
                                       justify="left", wraplength=760)
        self.lbl_estado.pack(side="left", fill="x", expand=True)
        self.btn_abrir = boton(fila1, "Abrir carpeta", self.abrir_ultima, principal=False, width=120,
                               state="disabled")
        self.btn_abrir.pack(side="right")
        self.btn_cancelar = boton(fila1, "Cancelar", self.cancelar, principal=False, width=100,
                                  state="disabled", text_color=C_ERROR, hover_color=C_ERROR_FONDO)
        self.btn_cancelar.pack(side="right", padx=8)
        self.barra = ctk.CTkProgressBar(t, height=8, corner_radius=4, progress_color="#E6E8F2",
                                        fg_color="#E6E8F2")
        self.barra.set(0)
        self.barra.pack(fill="x", padx=18, pady=(0, 10))
        fila2 = ctk.CTkFrame(t, fg_color="transparent")
        fila2.pack(fill="x", padx=18, pady=(0, 14))
        self.var_vinc = tk.BooleanVar(value=self.cfg.get("vinculados", False))
        ctk.CTkCheckBox(fila2, text="Incluir incidentes y expedientes vinculados", variable=self.var_vinc,
                        font=fuente(13), text_color=C_TEXTO, fg_color=C_ACENTO, hover_color=C_ACENTO_HOVER,
                        border_color="#B9BED6", checkbox_width=18, checkbox_height=18,
                        corner_radius=4).pack(side="left")
        self.var_destino = tk.StringVar(
            value=self.cfg.get("destino") or str(carpeta_documentos() / "Expedientes PJN"))
        boton(fila2, "Cambiar…", self.elegir_destino, principal=False, width=90, height=30).pack(side="right")
        self.lbl_destino = ctk.CTkLabel(fila2, text="", font=fuente(13), text_color=C_SUAVE)
        self.lbl_destino.pack(side="right", padx=8)
        self.var_destino.trace_add("write", lambda *a: self._mostrar_destino())
        self._mostrar_destino()
        ctk.CTkLabel(fila2, text="Se guarda en:", font=fuente(13, "bold"), text_color=C_TEXTO).pack(side="right")

    def _bajar_hasta_resultado(self):
        try:
            self.consulta_scroll._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _mostrar_destino(self):
        """Muestra la carpeta de destino abreviada (las dos últimas partes) para que no se pise."""
        partes = Path(self.var_destino.get()).parts
        corto = str(Path(*partes[-2:])) if len(partes) > 2 else self.var_destino.get()
        self.lbl_destino.configure(text=("…" + os.sep + corto) if len(partes) > 2 else corto)

    def mostrar_lista(self):
        if not getattr(self, "tree", None) or not self.tree.winfo_exists():
            return
        q = self.e_buscar.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        n = 0
        for i, e in enumerate(self.lista):
            texto = " ".join(e.values()).lower()
            if q and q not in texto and q not in pjn_core.normalizar_nro(e["expediente"]).lower():
                continue
            self.tree.insert("", "end", iid=str(i), tags=("par",) if n % 2 else (), values=(
                e["expediente"], e["caratula"], e["dependencia"], e["situacion"], e["ult_act"]))
            n += 1
        self.lbl_cuenta.configure(text=f"{n} de {len(self.lista)} causas")

    # --- acciones

    def set_ocupado(self, si):
        self.ocupado = si
        if getattr(self, "barra", None) is not None and self.barra.winfo_exists():
            if si:
                self.barra.configure(progress_color=C_ACENTO)
            elif self.barra.get() < 1:
                self.barra.set(0)
                self.barra.configure(progress_color="#E6E8F2")
        estado = "disabled" if si else "normal"
        for b in (self.btn_ingresar, self.btn_descargar, getattr(self, "btn_buscar", None)):
            if b is not None and b.winfo_exists():
                b.configure(state=estado)
        if self.btn_descargar is not None and self.btn_descargar.winfo_exists():
            self.btn_cancelar.configure(state="normal" if si else "disabled")
            self.btn_descargar_otra.configure(state=estado)
            for b in (self.btn_mev_ingresar, self.btn_mev_buscar, self.btn_mev_descargar):
                b.configure(state=estado)

    def estado(self, texto, error=False):
        if getattr(self, "lbl_estado", None) is not None and self.lbl_estado.winfo_exists():
            self.lbl_estado.configure(text=texto, text_color=C_ERROR if error else
                                      (C_SUAVE if self.btn_descargar is None else C_TEXTO))

    def ingresar(self):
        if self.ocupado:
            return
        usuario = "".join(ch for ch in self.e_usuario.get() if ch.isdigit())
        clave = self.e_clave.get()
        if not usuario or not clave:
            self.estado("Escriba su CUIT/CUIL y su clave.", error=True)
            return
        self._credenciales = (usuario, clave)
        self.usuario_actual = usuario
        self.set_ocupado(True)
        self.estado("Ingresando al PJN…")
        self.trabajador.ordenes.put(("ingresar", usuario, clave))

    def ingresar_manual(self):
        if self.ocupado:
            return
        self._credenciales = None
        self.usuario_actual = ""
        self.set_ocupado(True)
        self.estado("Se abre una ventana del navegador: ingrese ahí con su usuario y clave.")
        self.trabajador.ordenes.put(("ingresar_manual",))

    def listar(self):
        if self.ocupado:
            return
        self.set_ocupado(True)
        self.lbl_cuenta.configure(text="Cargando…")
        self.trabajador.ordenes.put(("listar", self.var_fuera.get()))

    def cambio_fuera(self):
        self.cfg["fuera_de_tramite"] = self.var_fuera.get()
        guardar_config(self.cfg)
        self.listar()

    def elegir_destino(self):
        d = filedialog.askdirectory(initialdir=self.var_destino.get(), title="¿Dónde guardar los expedientes?")
        if d:
            self.var_destino.set(d)
            self.cfg["destino"] = d
            guardar_config(self.cfg)

    def descargar(self):
        if self.ocupado:
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Expedientes PJN", "Primero haga clic en una causa de la lista.")
            return
        e = self.lista[int(sel[0])]
        vinc = self.var_vinc.get()
        if not messagebox.askyesno(
                "Descargar",
                f"¿Descargar todos los documentos de\n\n{e['expediente']}\n{e['caratula']}"
                + ("\n\n(incluyendo incidentes y vinculados)" if vinc else "") + "?"):
            return
        self._empezar_descarga(e["expediente"], vinc)

    def _numero_consulta(self):
        sigla = self.var_jur.get().split(" - ")[0].strip()
        num = "".join(ch for ch in self.e_num.get() if ch.isdigit())
        anio = "".join(ch for ch in self.e_anio.get() if ch.isdigit())
        if not num or len(anio) != 4:
            return None
        return f"{sigla} {int(num)}/{anio}"

    def buscar_causa(self):
        if self.ocupado:
            return
        numero = self._numero_consulta()
        if not numero:
            messagebox.showinfo("Expedientes PJN", "Escriba el número y el año (4 cifras, por ejemplo 2019).")
            return
        self.cfg["ultima_jurisdiccion"] = self.var_jur.get()
        guardar_config(self.cfg)
        self.causa_encontrada = None
        self.res.pack_forget()
        self.set_ocupado(True)
        self.estado(f"Buscando {numero}…")
        self.trabajador.ordenes.put(("consultar", numero))

    def descargar_otra(self):
        if self.ocupado or not self.causa_encontrada:
            return
        numero, datos = self.causa_encontrada
        vinc = self.var_vinc.get()
        if not messagebox.askyesno(
                "Descargar",
                f"¿Descargar todos los documentos de\n\n{datos.get('Expediente', numero)}\n"
                f"{datos.get('Carátula', '')}"
                + ("\n\n(incluyendo incidentes y vinculados)" if vinc else "") + "?"):
            return
        self._empezar_descarga(numero, vinc, solo_consulta=True)

    def _empezar_descarga(self, numero, vinc, solo_consulta=False):
        destino = self.var_destino.get()
        Path(destino).mkdir(parents=True, exist_ok=True)
        self.cfg["vinculados"] = vinc
        guardar_config(self.cfg)
        self.barra.set(0)
        self.set_ocupado(True)
        self.btn_abrir.configure(state="disabled")
        self.trabajador.ordenes.put(("descargar", numero, destino, vinc, solo_consulta))

    def cancelar(self):
        self.trabajador.cancelar.set()
        self.estado("Cancelando…")

    def abrir_ultima(self):
        if self.ultima_carpeta:
            abrir_carpeta(self.ultima_carpeta)

    def cerrar_sesion(self):
        if self.ocupado:
            return
        if self.var_recordar.get() and messagebox.askyesno(
                "Salir", "¿Olvidar también el usuario y la clave guardados en esta computadora?"):
            olvidar_clave()
            self.var_recordar.set(False)
        self.lista = []
        self.usuario_actual = ""
        self.pjn_conectado = False
        self.trabajador.credenciales = None
        if self.mev_conectada:
            self.pantalla_principal("Provincia (MEV)")
        else:
            self.pantalla_inicio()

    def salir(self):
        if self.ocupado and not messagebox.askyesno("Salir", "Hay una descarga en curso. ¿Salir igual?"):
            return
        self.trabajador.cancelar.set()
        self.trabajador.ordenes.put(("salir",))
        self.root.after(300, self.root.destroy)

    # --- eventos del trabajador

    def procesar_eventos(self):
        try:
            while True:
                ev = self.eventos.get_nowait()
                getattr(self, "ev_" + ev[0])(*ev[1:])
        except queue.Empty:
            pass
        self.root.after(100, self.procesar_eventos)

    def ev_progreso(self, texto, actual, total):
        self.estado(texto)
        if total and getattr(self, "barra", None) is not None and self.barra.winfo_exists():
            self.barra.set((actual or 0) / total)

    def ev_login_error(self, texto):
        self.set_ocupado(False)
        self.estado(f"No se pudo ingresar: {texto}", error=True)

    def ev_login_ok(self):
        if self._credenciales:
            if self.var_recordar.get():
                recordar_clave(*self._credenciales)
            else:
                olvidar_clave()
        self._credenciales = None
        self.set_ocupado(False)
        self.pjn_conectado = True
        self.pantalla_principal("Mis causas")
        self.listar()

    def ev_lista(self, lista):
        self.set_ocupado(False)
        self.lista = lista
        self.mostrar_lista()
        self.estado("Elija una causa de la lista y apriete «Descargar la causa elegida».")

    def ev_verificacion(self):
        self.estado("Se abrió una ventana del PJN con la causa ya cargada. Complete el verificador "
                    "(el cuadro de verificación) y apriete «Consultar». Después el programa sigue solo.")

    def ev_consulta(self, numero, datos):
        self.causa_encontrada = (numero, datos)
        self.set_ocupado(False)
        self.lbl_res_titulo.configure(text=f"Encontrada: {datos.get('Expediente', numero)}")
        self.lbl_res_caratula.configure(text=datos.get("Carátula", ""))
        detalle = "   ·   ".join(x for x in (datos.get("Dependencia", ""), datos.get("Sit. Actual", "")) if x)
        self.lbl_res_detalle.configure(text=detalle)
        self.res.pack(fill="x", pady=(16, 0))
        self.root.after(150, self._bajar_hasta_resultado)
        self.estado("Si es la causa correcta, apriete «Descargar esta causa».")

    def ev_mev_login_ok(self):
        if getattr(self, "_mev_cred", None):
            if self.var_mev_recordar.get():
                mev_recordar(*self._mev_cred)
            self.cfg["mev_creado_en"] = self._mev_cred[2]
            guardar_config(self.cfg)
        self.set_ocupado(False)
        self.mev_conectada = True
        self._mev_mostrar_busqueda()

    def _mev_mostrar_busqueda(self):
        self.mev_login.pack_forget()
        self.mev_busqueda.pack(fill="x")
        self._mostrar_recientes_mev()
        self.estado("Elija el departamento judicial y el juzgado, y busque la causa.")
        if self.var_mev_jur.get() != "Elija…":
            self.mev_cargar_organismos()

    def ev_mev_login_error(self, texto):
        self.set_ocupado(False)
        self.estado(f"No se pudo ingresar a la MEV: {texto}", error=True)

    def ev_mev_organismos(self, jurisdiccion, organismos):
        self.set_ocupado(False)
        self.mev_organismos = organismos
        nombres = [n for _, n in organismos] or ["(sin juzgados)"]
        self.menu_mev_org.configure(values=nombres)
        previo = self.cfg.get("mev_org")
        self.var_mev_org.set(previo if previo in nombres else nombres[0])
        self.estado(f"{len(organismos)} juzgados y organismos. Elija uno y busque la causa.")

    def ev_mev_resultados(self, items):
        self.set_ocupado(False)
        self._mostrar_mev_items(items, f"{len(items)} causa(s) encontradas" if items
                                else "No se encontraron causas con esos datos.")
        self.estado("Elija una causa y apriete «Descargar la causa elegida»." if items
                    else "La MEV busca solo dentro del juzgado elegido: fíjese que sea el juzgado de la causa. "
                         "En «Número» alcanza con el número solo (por ejemplo 1847).")

    def ev_mev_bajada(self, item):
        recientes = [r for r in self.cfg.get("mev_recientes", []) if r.get("nid") != item.get("nid")]
        recientes.insert(0, {k: item.get(k, "") for k in ("nid", "pid", "expediente", "caratula", "estado",
                                                           "inicio", "ultimo")})
        self.cfg["mev_recientes"] = recientes[:40]
        guardar_config(self.cfg)

    def ev_descarga_ok(self, carpeta, docs, errores):
        self.set_ocupado(False)
        self.ultima_carpeta = carpeta
        self.btn_abrir.configure(state="normal")
        if errores:
            self.estado(f"Terminó con {errores} documento(s) sin bajar. Vuelva a descargar la misma causa "
                        f"para reintentar (los ya bajados no se repiten).", error=True)
        else:
            self.barra.configure(progress_color="#2E9E4F")
            self.barra.set(1)
            self.estado(f"Listo: la carpeta tiene {docs} documento(s) de esta causa. {carpeta}")
        abrir_carpeta(carpeta)

    def ev_cancelado(self):
        self.set_ocupado(False)
        self.estado("Cancelado. Lo ya bajado quedó guardado.")

    def ev_error(self, orden, texto):
        self.set_ocupado(False)
        if orden in ("ingresar", "ingresar_manual"):
            self.pantalla_ingreso()
            self.estado(f"No se pudo ingresar: {texto}", error=True)
        elif texto == "SesionVencida":
            self.pjn_conectado = False
            self.pantalla_ingreso()
            self.estado("La sesión venció. Vuelva a ingresar.", error=True)
        elif orden.startswith("mev_"):
            if "MEV venció" in texto and getattr(self, "mev_login", None) is not None:
                self.mev_conectada = False
                self.mev_busqueda.pack_forget()
                self.mev_login.pack(fill="x")
            if orden == "mev_organismos":
                self.menu_mev_org.configure(values=["Primero elija el departamento"])
                self.var_mev_org.set("Primero elija el departamento")
            self.estado(f"MEV: {texto}", error=True)
        elif orden == "consultar":
            self.estado(f"No se encontró: {texto}", error=True)
        else:
            self.estado(f"Hubo un problema: {texto}", error=True)
            messagebox.showerror("Expedientes PJN",
                                 f"Hubo un problema (versión {VERSION}):\n\n{texto}\n\nDetalle en: {REGISTRO}")


def main():
    ctk.set_appearance_mode("light")
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
