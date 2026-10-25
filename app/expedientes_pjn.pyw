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
from playwright.sync_api import TimeoutError as PWTimeout  # noqa: E402

try:
    import keyring
except Exception:  # sin keyring no se puede recordar la clave
    keyring = None

SERVICIO = "ExpedientesPJN"
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
        self.credenciales = None  # solo en memoria, para reingresar si vence la sesión

    def avisar(self, texto, actual=None, total=None):
        print(texto, flush=True)
        self.eventos.put(("progreso", texto, actual, total))

    def _nuevo_pjn(self, visible):
        if self.pjn:
            self.pjn.cerrar()
        self.pjn = pjn_core.PJN(avisar=self.avisar, cancelar=self.cancelar, visible=visible)
        self.pjn.abrir_navegador()

    def run(self):
        while True:
            orden, *args = self.ordenes.get()
            if orden == "salir":
                if self.pjn:
                    self.pjn.cerrar()
                return
            self.cancelar.clear()
            try:
                getattr(self, "_" + orden)(*args)
            except pjn_core.Cancelado:
                self.eventos.put(("cancelado",))
            except Exception as e:
                traceback.print_exc()
                self.eventos.put(("error", orden, str(e) or e.__class__.__name__))

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
        self.avisar("Buscando sus expedientes…")
        lista = self.pjn.listar_mis_expedientes(fuera_de_tramite)
        self.eventos.put(("lista", lista))

    def _consultar(self, numero):
        self.avisar(f"Buscando {numero}…")
        for intento in range(2):
            try:
                datos = self.pjn.consultar(numero)
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
        for intento in range(1, intentos + 1):
            try:
                carpeta, docs, errores = self.pjn.descargar_expediente(
                    numero, destino, vinculados, solo_consulta)
                break
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

        root.title(f"Expedientes PJN — versión {VERSION}")
        root.configure(fg_color=C_FONDO)
        root.geometry("1180x760")
        root.minsize(940, 640)
        if sys.platform == "win32":  # customtkinter pone su ícono a los 200 ms: el nuestro va después
            root.after(300, lambda: self._poner_icono())
        self._estilo_tabla()

        self.marco = ctk.CTkFrame(root, fg_color="transparent")
        self.marco.pack(fill="both", expand=True)
        self.pantalla_ingreso()
        root.protocol("WM_DELETE_WINDOW", self.salir)
        root.after(100, self.procesar_eventos)

        usuario, clave = clave_guardada()
        if usuario and clave:
            self.e_usuario.insert(0, usuario)
            self.e_clave.insert(0, clave)
            self.var_recordar.set(True)
            root.after(400, self.ingresar)

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

        self.e_usuario.bind("<Return>", lambda e: self.e_clave.focus_set())
        self.e_clave.bind("<Return>", lambda e: self.ingresar())
        self.root.after(100, self._enfocar_ingreso)

    def _enfocar_ingreso(self):
        try:
            if self.e_usuario.winfo_exists():
                (self.e_clave if self.e_usuario.get() else self.e_usuario).focus_set()
        except tk.TclError:
            pass

    def pantalla_principal(self):
        self.limpiar()
        m = self.marco

        # Barra superior
        barra = ctk.CTkFrame(m, fg_color=C_TARJETA, corner_radius=0, height=68)
        barra.pack(fill="x")
        barra.pack_propagate(False)
        logo(barra, 38).pack(side="left", padx=(24, 12))
        ctk.CTkLabel(barra, text="Expedientes PJN", font=fuente(19, "bold"),
                     text_color=C_TEXTO).pack(side="left")
        boton(barra, "Salir de la cuenta", self.cerrar_sesion, principal=False).pack(side="right", padx=24)
        if self.usuario_actual:
            ctk.CTkLabel(barra, text=f"CUIT {self.usuario_actual}", font=fuente(13),
                         text_color=C_SUAVE).pack(side="right")
        ctk.CTkFrame(m, fg_color=C_BORDE, height=1, corner_radius=0).pack(fill="x")
        self._armar_pie(m)  # va primero y abajo: siempre queda visible entero

        cuerpo = ctk.CTkFrame(m, fg_color="transparent")
        cuerpo.pack(fill="both", expand=True, padx=24, pady=(18, 0))

        self.selector = ctk.CTkSegmentedButton(
            cuerpo, values=["Mis causas", "Consultar cualquier causa"], command=self._cambiar_seccion,
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

        self.selector.set("Mis causas")
        self._cambiar_seccion("Mis causas")

    def _ajustar_texto_selector(self):
        # El texto de la opción no elegida va en gris oscuro (la elegida, en blanco).
        try:
            for nombre, b in self.selector._buttons_dict.items():
                elegido = nombre == self.selector.get()
                b.configure(text_color="#FFFFFF" if elegido else C_TEXTO)
        except Exception:
            pass

    def _cambiar_seccion(self, valor):
        self.sec_mias.pack_forget()
        self.sec_otra.pack_forget()
        (self.sec_mias if valor == "Mis causas" else self.sec_otra).pack(fill="both", expand=True)
        self._ajustar_texto_selector()

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
        f = ctk.CTkFrame(sec, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=32, pady=22)
        ctk.CTkLabel(f, text="Consultar cualquier causa", font=fuente(20, "bold"),
                     text_color=C_TEXTO, anchor="w").pack(fill="x")
        ctk.CTkLabel(f, text="Sirve para causas que no son suyas. Elija la jurisdicción, escriba número y año, "
                             "y apriete «Buscar».", font=fuente(14), text_color=C_SUAVE,
                     anchor="w").pack(fill="x", pady=(2, 22))

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

        self.res = ctk.CTkFrame(f, fg_color=C_OK_FONDO, corner_radius=12)
        self.lbl_res_titulo = ctk.CTkLabel(self.res, text="", font=fuente(15, "bold"), anchor="w",
                                           justify="left", text_color=C_OK_TEXTO)
        self.lbl_res_titulo.pack(fill="x", padx=18, pady=(14, 2))
        self.lbl_res_caratula = ctk.CTkLabel(self.res, text="", font=fuente(14), anchor="w", justify="left",
                                             text_color=C_TEXTO, wraplength=820)
        self.lbl_res_caratula.pack(fill="x", padx=18)
        self.lbl_res_detalle = ctk.CTkLabel(self.res, text="", font=fuente(13), anchor="w", justify="left",
                                            text_color=C_SUAVE, wraplength=820)
        self.lbl_res_detalle.pack(fill="x", padx=18, pady=(2, 12))
        self.btn_descargar_otra = boton(self.res, "Descargar esta causa", self.descargar_otra, width=260)
        self.btn_descargar_otra.pack(anchor="w", padx=18, pady=(0, 16))
        return sec

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
        ctk.CTkLabel(fila2, textvariable=self.var_destino, font=fuente(13), text_color=C_SUAVE).pack(
            side="right", padx=8)
        ctk.CTkLabel(fila2, text="Se guarda en:", font=fuente(13, "bold"), text_color=C_TEXTO).pack(side="right")

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
        self.pantalla_ingreso()

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
        self.pantalla_principal()
        self.listar()

    def ev_lista(self, lista):
        self.set_ocupado(False)
        self.lista = lista
        self.mostrar_lista()
        self.estado("Elija una causa de la lista y apriete «Descargar la causa elegida».")

    def ev_consulta(self, numero, datos):
        self.causa_encontrada = (numero, datos)
        self.set_ocupado(False)
        self.lbl_res_titulo.configure(text=f"Encontrada: {datos.get('Expediente', numero)}")
        self.lbl_res_caratula.configure(text=datos.get("Carátula", ""))
        detalle = "   ·   ".join(x for x in (datos.get("Dependencia", ""), datos.get("Sit. Actual", "")) if x)
        self.lbl_res_detalle.configure(text=detalle)
        self.res.pack(fill="x", pady=(20, 0))
        self.estado("Si es la causa correcta, apriete «Descargar esta causa».")

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
            self.pantalla_ingreso()
            self.estado("La sesión venció. Vuelva a ingresar.", error=True)
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
