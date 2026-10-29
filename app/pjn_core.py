"""
Lógica de acceso al Sistema de Consulta Web (SCW) del PJN: ingreso, lista de
"Mis expedientes", lectura de actuaciones y descarga de documentos.

Todo corre en un único hilo (Playwright sync). El progreso se informa con un
callback `avisar(texto, actual=None, total=None)` y se puede cancelar con un
threading.Event.
"""

import csv
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

SCW = "https://scw.pjn.gov.ar/scw"
URL_LISTA = f"{SCW}/consultaListaRelacionados.seam"
URL_HOME = f"{SCW}/home.seam"
RE_SCW = re.compile(r"scw\.pjn\.gov\.ar/scw/")

SEL_LISTA = '[id^="tablaConsultaLista:tablaConsultaForm"][id$=":dataTable"]'
SEL_LISTA_SIG = '[id^="tablaConsultaLista"] [id$="divPagesAct"] ul.pagination span[title="Siguiente"]'
SEL_ACT = '[id="expediente:action-table"]'
SEL_ACT_SIG = '[id$="divPagesAct"] ul.pagination span[title="Siguiente"]'
SEL_HIST = '[id="expediente:action-historic-table"]'
SEL_HIST_SIG = '[id$="actuacionHistoricaTab"] ul.pagination a:has-text("Siguiente")'
SEL_VINC = '[id="expediente:connectedTable"]'
SEL_VINC_PAG = '[id$="vinculadosTab"] ul.pagination'

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")

JS_TEXTO = r"""
(c) => { if (!c) return ''; const k = c.cloneNode(true);
  k.querySelectorAll('.sr-only').forEach(e => e.remove());
  return k.innerText.replace(/\s+/g, ' ').trim(); }
"""

JS_LEER_ACTUACIONES = r"""
(sel) => {
  const t = document.querySelector(sel);
  if (!t || !t.tBodies.length) return [];
  const txt = """ + JS_TEXTO + r""";
  const heads = [...t.rows[0].cells].map(c => c.innerText.replace(/\s+/g, ' ').trim().toUpperCase());
  const col = n => heads.findIndex(h => h.startsWith(n));
  const ci = {of: col('OFICINA'), fe: col('FECHA'), ti: col('TIPO'), de: col('DESCRIPCION'), fs: col('A FS')};
  return [...t.tBodies[0].rows].map(r => {
    const dl = r.querySelector('a[href*="viewer.seam"][href*="download"]');
    const ver = r.querySelector('a[href*="viewer.seam"]:not([href*="download"])');
    return {oficina: txt(r.cells[ci.of]), fecha: txt(r.cells[ci.fe]), tipo: txt(r.cells[ci.ti]),
            detalle: txt(r.cells[ci.de]), fojas: txt(r.cells[ci.fs]),
            url: dl ? dl.href : (ver ? ver.href : null)};
  }).filter(x => /\d{1,2}\/\d{1,2}\/\d{4}/.test(x.fecha));
}
"""

JS_LEER_LISTA = r"""
(sel) => {
  const t = document.querySelector(sel);
  if (!t || !t.tBodies.length) return [];
  const txt = """ + JS_TEXTO + r""";
  const heads = [...t.rows[0].cells].map(c => c.innerText.replace(/\s+/g, ' ').trim().toUpperCase());
  const col = n => heads.findIndex(h => h.startsWith(n));
  const ci = {ex: col('EXPEDIENTE'), de: col('DEPENDENCIA'), ca: col('CARÁTULA'), si: col('SITUACI'), ul: col('ÚLT')};
  return [...t.tBodies[0].rows].map(r => ({
    expediente: txt(r.cells[ci.ex]), dependencia: txt(r.cells[ci.de]), caratula: txt(r.cells[ci.ca]),
    situacion: txt(r.cells[ci.si]), ult_act: txt(r.cells[ci.ul]),
  })).filter(x => x.expediente);
}
"""

# Completa el filtro de "Mis expedientes" (puede estar plegado/oculto). Devuelve 'ok'.
JS_FILTRO = r"""
([sigla, num, anio, verTodos]) => {
  const f = document.querySelector('[id$="consultaExpediente"]');
  if (!f) return 'sin-filtro';
  if (sigla) {
    const sel = f.querySelector('select[id$=":camara"]');
    const opt = sel && [...sel.options].find(o => o.value && o.textContent.trim().toUpperCase().startsWith(sigla + ' '));
    if (!opt) return 'sin-jurisdiccion';
    sel.value = opt.value;
    f.querySelector('input[id$=":numero"]').value = num;
    f.querySelector('input[id$=":anio"]').value = anio;
  }
  const cb = f.querySelector('input[type=checkbox]');
  if (cb && verTodos && !cb.checked) cb.click();
  return 'ok';
}
"""

JS_TBODY = "(sel) => { const t = document.querySelector(sel); return t && t.tBodies.length ? t.tBodies[0].innerText : ''; }"
JS_CAMBIO = ("([sel, prev]) => { const t = document.querySelector(sel);"
             " return !!t && t.tBodies.length > 0 && t.tBodies[0].innerText !== prev; }")


# Jurisdicciones de la Consulta pública del SCW (sigla, nombre).
JURISDICCIONES = [
    ("CSJ", "Corte Suprema de Justicia de la Nación"),
    ("CIV", "Cámara Nacional de Apelaciones en lo Civil"),
    ("CAF", "Contencioso Administrativo Federal"),
    ("CCF", "Civil y Comercial Federal"),
    ("CNE", "Cámara Nacional Electoral"),
    ("CSS", "Cámara Federal de la Seguridad Social"),
    ("CPE", "Penal Económico"),
    ("CNT", "Cámara Nacional de Apelaciones del Trabajo"),
    ("CFP", "Criminal y Correccional Federal"),
    ("CCC", "Criminal y Correccional"),
    ("COM", "Cámara Nacional de Apelaciones en lo Comercial"),
    ("CPF", "Cámara Federal de Casación Penal"),
    ("CPN", "Cámara Nacional de Casación Penal"),
    ("FBB", "Justicia Federal de Bahía Blanca"),
    ("FCR", "Justicia Federal de Comodoro Rivadavia"),
    ("FCB", "Justicia Federal de Córdoba"),
    ("FCT", "Justicia Federal de Corrientes"),
    ("FGR", "Justicia Federal de General Roca"),
    ("FLP", "Justicia Federal de La Plata"),
    ("FMP", "Justicia Federal de Mar del Plata"),
    ("FMZ", "Justicia Federal de Mendoza"),
    ("FPO", "Justicia Federal de Posadas"),
    ("FPA", "Justicia Federal de Paraná"),
    ("FRE", "Justicia Federal de Resistencia"),
    ("FSA", "Justicia Federal de Salta"),
    ("FRO", "Justicia Federal de Rosario"),
    ("FSM", "Justicia Federal de San Martín"),
    ("FTU", "Justicia Federal de Tucumán"),
]


class Cancelado(Exception):
    pass


class ErrorLogin(Exception):
    pass


class LoginManual(Exception):
    """El SSO pidió algo más que usuario y clave (cambio de clave, términos, etc.)."""


class SesionVencida(Exception):
    pass


class NecesitaVerificacion(Exception):
    """La Consulta pública del PJN pide completar el verificador (captcha)."""


class SinPermiso(Exception):
    """El PJN no deja ver ese documento con esta cuenta."""


class PaginaNoCargo(Exception):
    """El PJN no devolvió una página de actuaciones ni después de reintentar."""


# ----------------------------------------------------------------------------- utilidades

def limpiar_nombre(s, maxlen=120):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:maxlen].rstrip(" .")


def fecha_iso(f):
    try:
        return dt.datetime.strptime(f, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return f.replace("/", "-")


def normalizar_nro(s):
    """'COM 012345/2020/2' -> 'COM 12345/2020/2' para comparar."""
    s = re.sub(r"\s+", " ", (s or "").strip().upper())
    return re.sub(r"(\D)0+(\d)", r"\1\2", s)


def parsear_expediente(texto):
    """'COM 012345/2020/2' -> ('COM', '12345', '2020', '2'); sin sufijo -> None."""
    m = re.match(r"^\s*([A-Za-z]{3})\W*0*(\d+)\s*[/\-\s]\s*(\d{4})(?:\s*/\s*(\S+))?\s*$", texto or "")
    if not m:
        raise ValueError(f'No entiendo el número de expediente "{texto}"')
    sigla, num, anio, sufijo = m.groups()
    return sigla.upper(), num, anio, sufijo


# ----------------------------------------------------------------------------- sesión

class PJN:
    def __init__(self, avisar=None, cancelar=None, visible=False, pw=None):
        self.avisar = avisar or (lambda texto, actual=None, total=None: print(texto, flush=True))
        self.cancelar = cancelar
        self.visible = visible
        self._pw = pw  # si viene de afuera (compartido con la MEV), no se apaga al cerrar
        self._pw_propio = pw is None
        self.browser = None
        self.ctx = None
        self.page = None

    # --- ciclo de vida

    def _lanzar(self, visible):
        opciones = dict(headless=not visible)
        browser = None
        if sys.platform == "win32":
            try:  # Edge viene con Windows: no hace falta bajar Chromium
                browser = self._pw.chromium.launch(channel="msedge", **opciones)
            except Exception:
                browser = None
        if browser is None:
            browser = self._pw.chromium.launch(**opciones)
        ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900}, locale="es-AR")
        page = ctx.new_page()
        page.set_default_timeout(90000)
        return browser, ctx, page

    def abrir_navegador(self):
        if self._pw is None:
            self._pw = sync_playwright().start()
        self.browser, self.ctx, self.page = self._lanzar(self.visible)
        self._oculto = None  # navegador invisible guardado mientras se usa uno visible

    def cerrar(self):
        if getattr(self, "_oculto", None):
            for cosa in self._oculto[:2]:
                try:
                    cosa.close()
                except Exception:
                    pass
            self._oculto = None
        for cosa in (self.ctx, self.browser):
            try:
                if cosa:
                    cosa.close()
            except Exception:
                pass
        try:
            if self._pw and self._pw_propio:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self.browser = self.ctx = self.page = None

    def _chequear_cancelado(self):
        if self.cancelar is not None and self.cancelar.is_set():
            raise Cancelado()

    # --- ingreso

    def ingresar(self, usuario, clave):
        """Ingresa con usuario y clave. Lanza ErrorLogin o LoginManual."""
        page = self.page
        page.goto(URL_LISTA, wait_until="domcontentloaded")
        if "sso.pjn.gov.ar" not in page.url:
            return
        page.wait_for_selector("#username", timeout=30000)
        page.fill("#username", usuario)
        page.fill("#password", clave)
        page.click("#kc-login")
        try:
            page.wait_for_url(RE_SCW, timeout=30000)
            return
        except PWTimeout:
            pass
        errores = page.locator(
            "#input-error, .kc-feedback-text, .alert-error, .pf-c-alert__title, .pf-m-error"
        ).all_inner_texts()
        errores = " ".join(e.strip() for e in errores if e.strip())
        if errores or page.locator("#password").count():
            raise ErrorLogin(errores or "Usuario o clave incorrectos.")
        raise LoginManual()

    def esperar_ingreso_manual(self, minutos=30):
        """Con navegador visible: deja que la persona ingrese a mano."""
        self.page.goto(URL_LISTA, wait_until="domcontentloaded")
        if "sso.pjn.gov.ar" in self.page.url:
            self.page.bring_to_front()
            self.page.wait_for_url(RE_SCW, timeout=minutos * 60 * 1000)

    # --- lista de expedientes

    def listar_mis_expedientes(self, incluir_fuera_de_tramite=False):
        page = self.page
        page.goto(URL_LISTA, wait_until="domcontentloaded")
        if "sso.pjn.gov.ar" in page.url:
            raise SesionVencida()
        if incluir_fuera_de_tramite:
            page.evaluate(JS_FILTRO, [None, "", "", True])
            self._enviar_filtro()
        try:
            page.wait_for_selector(SEL_LISTA, timeout=30000)
        except PWTimeout:
            return []
        return self._paginar(SEL_LISTA, SEL_LISTA_SIG, JS_LEER_LISTA, "Lista de expedientes")

    # --- navegación de tablas

    def _paginar(self, sel_tabla, sel_siguiente, js_leer, etiqueta):
        page = self.page
        filas, vistas, n = [], set(), 1
        while True:
            self._chequear_cancelado()
            page.wait_for_selector(sel_tabla, timeout=30000)
            previo = page.evaluate(JS_TBODY, sel_tabla)
            if previo in vistas:
                break
            vistas.add(previo)
            nuevas = page.evaluate(js_leer, sel_tabla)
            filas.extend(nuevas)
            self.avisar(f"{etiqueta}: página {n}")
            sig = page.locator(sel_siguiente)
            if sig.count() == 0:
                break
            sig.first.evaluate("e => (e.closest('a') || e).click()")
            try:
                page.wait_for_function(JS_CAMBIO, arg=[sel_tabla, previo], timeout=60000)
            except PWTimeout:
                self.avisar(f"{etiqueta}: la página {n + 1} no cargó; sigo con lo que hay.")
                break
            page.wait_for_timeout(300)
            n += 1
        return filas

    def _esperar_expediente(self, objetivo=None, timeout=90000):
        """Espera a que el expediente esté cargado (tolera que la página se esté recargando)."""
        ok = self._esperar(
            """(obj) => {
                 if (!document.body || document.readyState === 'loading') return false;
                 if (!document.querySelector('[id="expediente:action-table"]') &&
                     !/El expediente no posee actuaciones/i.test(document.body.innerText)) return false;
                 if (!obj) return true;
                 const m = document.body.innerText.match(/Expediente:\\s*([^\\n(]+)/);
                 if (!m) return false;
                 const s = m[1].trim().toUpperCase().replace(/\\s+/g, ' ').replace(/(\\D)0+(\\d)/g, '$1$2');
                 return s === obj;
               }""", objetivo, segundos=timeout / 1000)
        if not ok:
            raise PWTimeout(f"El expediente {objetivo or ''} no terminó de cargar.")
        self.page.wait_for_timeout(400)

    # --- abrir un expediente

    def abrir_expediente(self, numero, solo_consulta=False):
        """Abre un expediente por su número (ej. 'COM 012345/2020' o 'COM 012345/2020/2').

        Con solo_consulta=True no usa la lista de Mis expedientes: va directo a la
        Consulta pública (sirve para cualquier causa, no solo las propias)."""
        sigla, num, anio, sufijo = parsear_expediente(numero)
        objetivo = normalizar_nro(numero)
        # Solo con la ventana del verificador: la causa ya está abierta ahí y volver a buscarla
        # pediría el verificador otra vez.
        if getattr(self, "_oculto", None) is not None and self._expediente_en_pantalla() == objetivo:
            self._volver_a_actuaciones()
            return
        if not solo_consulta and self._abrir_desde_lista(sigla, num, anio, objetivo):
            return
        if not sufijo:
            self._abrir_por_consulta(sigla, num, anio, objetivo)
            return
        # Incidente que no está en la lista: entrar por el principal -> Vinculados.
        principal = f"{sigla} {num}/{anio}"
        self.abrir_expediente(principal, solo_consulta)
        for v in self.listar_vinculados():
            if normalizar_nro(v["expediente"]) == objetivo:
                self.abrir_vinculado(v, principal, solo_consulta)
                return
        raise RuntimeError(f"No encontré {numero} entre los vinculados de {principal}.")

    def diagnostico(self, msg):
        print(f"[diagnóstico] {msg}", flush=True)  # queda en registro.txt

    def _expediente_en_pantalla(self, page=None):
        """Número del expediente que muestra la página ahora (o None)."""
        page = page or self.page
        try:
            if "/expediente.seam" not in page.url and "actuacionesHistoricas" not in page.url:
                return None
            texto = page.evaluate("() => document.body ? document.body.innerText : ''")
            m = re.search(r"Expediente:\s*([^\n(]+)", texto)
            if not m:
                self.diagnostico(f"página de expediente sin 'Expediente:' ({page.url.split('?')[0]}): "
                                 f"{texto[:300]!r}")
                return None
            return normalizar_nro(m.group(1))
        except Exception as e:
            self.diagnostico(f"no se pudo leer la página ({type(e).__name__}: {e})")
            return None

    def _volver_a_actuaciones(self):
        """Si quedó en las históricas, vuelve a la vista principal del expediente."""
        if "actuacionesHistoricas" in self.page.url:
            volver = self.page.locator("a", has_text="Volver al expediente")
            if volver.count():
                volver.first.evaluate("a => a.click()")
                self.page.wait_for_url(re.compile(r"/expediente\.seam"), timeout=90000)
                self._esperar_expediente()

    def consultar_con_verificacion(self, numero, minutos=15):
        """Abre una ventana visible del PJN con la consulta cargada para que la persona complete
        el verificador. Cuando aparece el expediente, el programa sigue con esa ventana."""
        sigla, num, anio, _ = parsear_expediente(numero)
        objetivo = normalizar_nro(numero)
        if self._oculto is not None and self.page.is_closed():  # cerraron la ventana: se abre otra
            for cosa in (self.ctx, self.browser):
                try:
                    cosa.close()
                except Exception:
                    pass
            self.browser, self.ctx, self.page = self._oculto
            self._oculto = None
        if self._oculto is None and not self.visible:  # si ya se ve una ventana, se usa esa
            cookies = self.ctx.cookies()
            visible = self._lanzar(True)
            try:
                visible[1].add_cookies(cookies)  # misma sesión: no hay que volver a ingresar
            except Exception:
                pass
            self._oculto = (self.browser, self.ctx, self.page)
            self.browser, self.ctx, self.page = visible
        page = self.page
        page.goto(URL_HOME, wait_until="domcontentloaded")
        page.wait_for_selector('[id="formPublica:camaraNumAni"]', state="attached", timeout=60000)
        page.evaluate(
            """([sigla, num, anio]) => {
                 const sel = document.getElementById('formPublica:camaraNumAni');
                 const opt = [...sel.options].find(o => o.value && o.textContent.trim().toUpperCase().startsWith(sigla + ' '));
                 if (opt) sel.value = opt.value;
                 document.getElementById('formPublica:numero').value = num;
                 document.getElementById('formPublica:anio').value = anio;
               }""", [sigla, num, anio])
        try:
            page.bring_to_front()
        except Exception:
            pass
        self.avisar("Se abrió una ventana del PJN: complete el verificador y apriete «Consultar». "
                    "El programa sigue solo.")
        fin = time.time() + minutos * 60
        ultimo_diag = 0
        while time.time() < fin:
            self._chequear_cancelado()
            if time.time() - ultimo_diag > 10:
                ultimo_diag = time.time()
                todas = [(pg.url.split("?")[0], self._expediente_en_pantalla(pg)) for pg in self.ctx.pages
                         if not pg.is_closed()]
                self.diagnostico(f"esperando {objetivo}; páginas: {todas}")
            # La causa puede aparecer en cualquier ventana o pestaña del programa.
            for pg in list(self.ctx.pages):
                if pg is not self.page and not pg.is_closed() and "/expediente.seam" in pg.url:
                    self.page = pg
            if self._oculto is not None:
                for pg in list(self._oculto[1].pages):
                    if not pg.is_closed() and "/expediente.seam" in pg.url:
                        # Se consultó en la otra ventana: se sigue con esa y se cierra la nueva.
                        for cosa in (self.ctx, self.browser):
                            try:
                                cosa.close()
                            except Exception:
                                pass
                        self.browser, self.ctx, self.page = self._oculto
                        self._oculto = None
                        self.page = pg
                        break
            if self._expediente_en_pantalla() == objetivo:
                self._esperar_expediente(objetivo)
                return self.datos_generales()
            if "sso.pjn.gov.ar" in page.url:
                self.avisar("Ingrese al PJN en esa misma ventana y después complete la consulta.")
            time.sleep(1)
        raise RuntimeError("No se completó la consulta en la ventana del PJN.")

    def volver_a_invisible(self):
        """Cierra la ventana visible del PJN y sigue con el navegador invisible."""
        if getattr(self, "_oculto", None):
            for cosa in (self.ctx, self.browser):
                try:
                    cosa.close()
                except Exception:
                    pass
            self.browser, self.ctx, self.page = self._oculto
            self._oculto = None

    def consultar(self, numero):
        """Abre cualquier causa por la Consulta pública y devuelve sus datos generales."""
        self.abrir_expediente(numero, solo_consulta=True)
        return self.datos_generales()

    def _esperar(self, js, arg=None, segundos=30):
        """Repite `js` hasta que devuelva algo verdadero (tolera recargas de la página)."""
        fin = time.time() + segundos
        while time.time() < fin:
            self._chequear_cancelado()
            try:
                r = self.page.evaluate(js, arg)
                if r:
                    return r
            except Exception:
                pass  # la página se está recargando
            time.sleep(0.5)
        return None

    def _enviar_filtro(self):
        """Aprieta 'Consultar' del filtro de Mis expedientes (aunque el filtro esté plegado)."""
        page = self.page
        page.evaluate(f"""() => {{
            window.__pjnViejo = true;
            const t = document.querySelector('{SEL_LISTA}');
            window.__pjnPrevio = t && t.tBodies.length ? t.tBodies[0].innerText : '';
            document.querySelector('[id$="consultaFiltroSearchButtonSAU"]').click(); }}""")
        time.sleep(1)
        self._esperar(f"""() => {{
            const t = document.querySelector('{SEL_LISTA}');
            const cambio = !window.__pjnViejo || (t && t.tBodies.length && t.tBodies[0].innerText !== window.__pjnPrevio);
            return document.readyState === 'complete' && cambio &&
                   (!!t || /no se encontraron|sin resultados/i.test(document.body.innerText));
        }}""", segundos=40)

    def _abrir_desde_lista(self, sigla, num, anio, objetivo):
        """Busca el expediente con el filtro de Mis expedientes y lo abre. False si no lo encuentra."""
        page = self.page
        page.goto(URL_LISTA, wait_until="domcontentloaded")
        if "sso.pjn.gov.ar" in page.url:
            raise SesionVencida()
        try:
            if page.evaluate(JS_FILTRO, [sigla, num, anio, True]) != "ok":
                return False
            self._enviar_filtro()
            ok = self._esperar(
                """([sel, obj]) => {
                     const t = document.querySelector(sel);
                     if (!t || !t.tBodies.length) return false;
                     const norm = s => s.trim().toUpperCase().replace(/\\s+/g, ' ').replace(/(\\D)0+(\\d)/g, '$1$2');
                     for (const r of t.tBodies[0].rows) {
                       const b = r.querySelector('a.btn');
                       if (r.cells[0] && b && norm(r.cells[0].innerText) === obj) { b.click(); return true; }
                     }
                     return false;
                   }""", [SEL_LISTA, objetivo], segundos=20)
            if not ok:
                return False
            page.wait_for_url(re.compile(r"/expediente\.seam"), timeout=45000)
            self._esperar_expediente(objetivo)
            return True
        except (SesionVencida, Cancelado):
            raise
        except Exception as e:
            self.avisar(f"No se pudo abrir desde la lista ({e.__class__.__name__}); pruebo con la consulta pública…")
            return False

    def _abrir_por_consulta(self, sigla, num, anio, objetivo):
        page = self.page
        page.goto(URL_HOME, wait_until="domcontentloaded")
        page.wait_for_selector('[id="formPublica:camaraNumAni"]', state="attached", timeout=30000)
        r = page.evaluate(
            """([sigla, num, anio]) => {
                 const sel = document.getElementById('formPublica:camaraNumAni');
                 const opt = [...sel.options].find(o => o.value && o.textContent.trim().toUpperCase().startsWith(sigla + ' '));
                 if (!opt) return 'sin-jurisdiccion';
                 sel.value = opt.value;
                 document.getElementById('formPublica:numero').value = num;
                 document.getElementById('formPublica:anio').value = anio;
                 document.getElementById('formPublica:buscarPorNumeroButton').click();
                 return 'ok';
               }""", [sigla, num, anio])
        if r != "ok":
            raise RuntimeError(f"Jurisdicción {sigla} desconocida.")
        time.sleep(2)
        r = self._esperar(
            """() => {
                 if (/\\/expediente\\.seam/.test(location.pathname)) return 'ok';
                 if (document.readyState !== 'complete') return false;
                 const errs = [...document.querySelectorAll(
                     '.ui-messages-info-summary, .ui-messages-warn-summary, .ui-messages-error-summary, ' +
                     '.ui-messages-fatal-summary, .rf-msgs-sum, .rf-msg-err, .rf-msgs-err, .rf-msg-det, ' +
                     '.alert-danger, .alert-warning, .ui-message-error, .text-danger')]
                   .filter(e => e.offsetParent !== null)
                   .map(e => e.innerText.replace(/\\s+/g, ' ').trim()).filter(t => t.length > 5);
                 return errs.length ? 'error:' + [...new Set(errs)].join(' | ') : false;
               }""", segundos=45)
        if r == "ok":
            self._esperar_expediente(objetivo)
            return
        if r and r.startswith("error:"):
            if re.search(r"verificador|captcha", r, re.I):
                raise NecesitaVerificacion()
            raise RuntimeError(f"{sigla} {num}/{anio}: {r[6:]}")
        raise RuntimeError(f"No se pudo abrir el expediente {sigla} {num}/{anio}. "
                           "Revise la jurisdicción, el número y el año.")

    @staticmethod
    def _valor_jurisdiccion(select, sigla):
        opciones = select.locator("option").evaluate_all(
            "os => os.map(o => [o.value, o.textContent.trim()])")
        return next((v for v, t in opciones if v and t.upper().startswith(sigla + " ")), None)

    def datos_generales(self):
        texto = self.page.locator("body").inner_text()
        datos = {}
        for etiqueta in ["Expediente", "Jurisdicción", "Dependencia", "Sit. Actual", "Carátula"]:
            m = re.search(re.escape(etiqueta) + r":\s*([^\n]+)", texto)
            if m:
                datos[etiqueta] = re.sub(r"\s*\(agregar/quitar a mis expedientes\)", "", m.group(1)).strip()
        return datos

    # --- actuaciones

    def _esperar_cambio(self, sel_tabla, previo, segundos=60):
        return self._esperar(JS_CAMBIO, [sel_tabla, previo], segundos=segundos)

    def _recorrer(self, sel_tabla, sel_siguiente, etiqueta, procesar, ir_a_pagina):
        """Recorre una tabla de actuaciones página por página.

        procesar(filas, n) se llama con cada página apenas se lee; si devuelve False se corta.
        Si el PJN no devuelve la página siguiente, reintenta y, si hace falta, usa
        ir_a_pagina(n) (que reabre el expediente y vuelve a esa página)."""
        page = self.page
        vistas, n = set(), 1
        while True:
            self._chequear_cancelado()
            page.wait_for_selector(sel_tabla, timeout=90000)
            previo = page.evaluate(JS_TBODY, sel_tabla)
            if previo in vistas:
                return
            vistas.add(previo)
            filas = page.evaluate(JS_LEER_ACTUACIONES, sel_tabla)
            if procesar(filas, n) is False:
                return
            if page.locator(sel_siguiente).count() == 0:
                return
            ok = False
            for intento in range(3):
                self._chequear_cancelado()
                sig = page.locator(sel_siguiente)
                if sig.count():
                    sig.first.evaluate("e => (e.closest('a') || e).click()")
                if self._esperar_cambio(sel_tabla, previo):
                    ok = True
                    break
                self.avisar(f"{etiqueta}: el PJN tarda en mostrar la página {n + 1}; reintento ({intento + 1}/3)…")
            if not ok:
                self.avisar(f"{etiqueta}: reabro el expediente para seguir desde la página {n + 1}…")
                try:
                    ir_a_pagina(n + 1)
                except (SesionVencida, Cancelado):
                    raise
                except Exception as e:
                    raise PaginaNoCargo(f"El PJN no respondió en la página {n + 1} de {etiqueta.lower()} ({e}).")
            page.wait_for_timeout(300)
            n += 1

    def _ir_a_pagina_actuales(self, reabrir, k):
        reabrir()
        page = self.page
        ul = '[id$="divPagesAct"] ul.pagination'
        for _ in range(500):
            self._chequear_cancelado()
            activa = page.locator(f"{ul} li.active").first
            if activa.count() and activa.inner_text().strip() == str(k):
                return
            links = page.locator(f"{ul} a")
            textos = [t.strip() for t in links.all_inner_texts()]
            nums = [(int(t), i) for i, t in enumerate(textos) if t.isdigit()]
            if str(k) in textos:
                destino = links.nth(textos.index(str(k)))
            elif nums and max(nums)[0] < k:
                destino = links.nth(max(nums)[1])
            else:
                raise PaginaNoCargo(f"no encuentro la página {k}")
            previo = page.evaluate(JS_TBODY, SEL_ACT)
            destino.evaluate("a => a.click()")
            if not self._esperar_cambio(SEL_ACT, previo):
                raise PaginaNoCargo(f"no cargó la página {k}")
            page.wait_for_timeout(300)
        raise PaginaNoCargo(f"no llegué a la página {k}")

    def _abrir_historicas(self):
        page = self.page
        link = page.locator("a", has_text=re.compile(r"^\s*Ver históricas\s*$"))
        if link.count() == 0:
            return False
        link.first.evaluate("a => a.click()")
        page.wait_for_url(re.compile(r"actuacionesHistoricas\.seam"), timeout=90000)
        if not self._esperar(
                "() => !!document.body && document.readyState !== 'loading' &&"
                " (!!document.querySelector('[id=\"expediente:action-historic-table\"]')"
                " || /no posee actuaciones hist/i.test(document.body.innerText))", segundos=90):
            raise PWTimeout("Las actuaciones históricas no terminaron de cargar.")
        return page.locator(SEL_HIST).count() > 0

    def _ir_a_pagina_historicas(self, reabrir, k):
        reabrir()
        if not self._abrir_historicas():
            raise PaginaNoCargo("no se pudieron abrir las históricas")
        for i in range(1, k):
            self._chequear_cancelado()
            previo = self.page.evaluate(JS_TBODY, SEL_HIST)
            sig = self.page.locator(SEL_HIST_SIG)
            if sig.count() == 0:
                raise PaginaNoCargo(f"no hay página {k}")
            sig.first.evaluate("e => (e.closest('a') || e).click()")
            if not self._esperar_cambio(SEL_HIST, previo):
                raise PaginaNoCargo(f"no cargó la página {i + 1}")
            self.page.wait_for_timeout(300)

    # --- vinculados

    def _abrir_tab_vinculados(self):
        hdr = self.page.locator('[id$=":header:inactive"]', has_text=re.compile(r"^\s*Vinculados\s*$"))
        if hdr.count():
            hdr.first.evaluate("e => e.click()")
        try:
            self.page.wait_for_selector(SEL_VINC, timeout=20000)
            return True
        except PWTimeout:
            return False

    def _ir_a_pagina_vinculados(self, n):
        page = self.page
        for _ in range(200):
            activa = page.locator(f"{SEL_VINC_PAG} li.active").first
            if activa.count() == 0 or activa.inner_text().strip() == str(n):
                return
            previo = page.evaluate(JS_TBODY, SEL_VINC)
            links = page.locator(f"{SEL_VINC_PAG} a")
            textos = [t.strip() for t in links.all_inner_texts()]
            if str(n) in textos:
                destino = links.nth(textos.index(str(n)))
            else:
                nums = [(int(t), i) for i, t in enumerate(textos) if t.isdigit()]
                if not nums:
                    return
                destino = links.nth(max(nums)[1])
            destino.evaluate("a => a.click()")
            page.wait_for_function(JS_CAMBIO, arg=[SEL_VINC, previo], timeout=60000)
            page.wait_for_timeout(300)

    def listar_vinculados(self):
        page = self.page
        if not self._abrir_tab_vinculados():
            return []
        lista, pagina, vistas = [], 1, set()
        while True:
            self._chequear_cancelado()
            cuerpo = page.evaluate(JS_TBODY, SEL_VINC)
            if cuerpo in vistas:
                break
            vistas.add(cuerpo)
            filas = page.evaluate(
                """(sel) => [...document.querySelector(sel).tBodies[0].rows].map((r, i) => ({
                     fila: i, expediente: r.cells[0] ? r.cells[0].innerText.trim() : '',
                     caratula: r.cells[3] ? r.cells[3].innerText.trim() : '',
                     boton: !!r.querySelector('a.btn')}))""", SEL_VINC)
            lista += [{"pagina": pagina, **f} for f in filas if f["boton"] and f["expediente"]]
            textos = [t.strip() for t in page.locator(f"{SEL_VINC_PAG} a").all_inner_texts()]
            if not any(t.isdigit() and int(t) > pagina for t in textos):
                break
            pagina += 1
            try:
                self._ir_a_pagina_vinculados(pagina)
            except PWTimeout:
                break
        return lista

    def abrir_vinculado(self, v, principal, solo_consulta=False):
        self.abrir_expediente(principal, solo_consulta)
        if not self._abrir_tab_vinculados():
            raise RuntimeError("No se pudo abrir la pestaña Vinculados.")
        if v["pagina"] > 1:
            self._ir_a_pagina_vinculados(v["pagina"])
        fila = self.page.locator(f"{SEL_VINC} tbody tr").nth(v["fila"])
        if normalizar_nro(fila.locator("td").first.inner_text()) != normalizar_nro(v["expediente"]):
            raise RuntimeError("La lista de vinculados cambió mientras se descargaba.")
        fila.locator("a.btn").first.evaluate("a => a.click()")
        self._esperar_expediente(normalizar_nro(v["expediente"]))

    # --- descarga

    def _bajar(self, url, intentos=2, segundos=60):
        """Baja un archivo con la sesión del navegador.

        No hay límite de tiempo total: mientras sigan llegando datos se espera lo que haga falta.
        Se abandona un intento solo si el PJN no manda nada durante `segundos` seguidos."""
        ultimo = None
        for intento in range(intentos):
            self._chequear_cancelado()
            try:
                return self._bajar_una_vez(url, segundos)
            except (SesionVencida, Cancelado, SinPermiso):
                raise
            except Exception as e:
                txt = str(e)
                ultimo = "el PJN dejó de enviar datos" if ("timed out" in txt or "imeout" in txt) else txt
            time.sleep(3 * (intento + 1))
        raise RuntimeError(ultimo)

    def _bajar_una_vez(self, url, segundos, profundidad=0):
        import urllib.request
        cookies = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies(url))
        req = urllib.request.Request(url, headers={"Cookie": cookies, "User-Agent": USER_AGENT,
                                                   "Accept": "*/*", "Referer": SCW + "/expediente.seam"})
        with urllib.request.urlopen(req, timeout=segundos, context=_contexto_ssl()) as r:  # timeout = sin datos
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct == "text/html":
                html = r.read(5 * 1024 * 1024)
                return self._respuesta_html(r.geturl(), html, segundos, profundidad)
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            total = int(r.headers.get("Content-Length") or 0)
            partes, recibido, ultimo_aviso = [], 0, time.time()
            while True:
                self._chequear_cancelado()
                bloque = r.read(256 * 1024)
                if not bloque:
                    break
                partes.append(bloque)
                recibido += len(bloque)
                if time.time() - ultimo_aviso > 3 and recibido > 2 * 1024 * 1024:
                    mb = recibido / 1048576
                    self.avisar(f"   … {mb:.0f} MB" + (f" de {total / 1048576:.0f} MB" if total else "")
                                + " (archivo grande, el PJN lo manda despacio)")
                    ultimo_aviso = time.time()
            if total and recibido < total:
                raise RuntimeError("el archivo llegó incompleto")
            cd = r.headers.get("Content-Disposition") or ""
            m = re.search(r'filename\*?="?([^";]+)"?', cd)
            ext = Path(m.group(1)).suffix.lower() if m else ""
            return b"".join(partes), ext or EXTENSIONES.get(ct, ".pdf")

    def _sesion_vigente(self):
        """Pregunta al PJN (sin tocar la página abierta) si la sesión sigue activa."""
        import urllib.request
        try:
            cookies = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies(URL_LISTA))
            req = urllib.request.Request(URL_LISTA, headers={"Cookie": cookies, "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60, context=_contexto_ssl()) as r:
                return "sso.pjn.gov.ar" not in r.geturl()
        except Exception:
            return True  # ante la duda, no se reinicia todo por un documento

    def _respuesta_html(self, url_final, html, segundos, profundidad):
        """El PJN devolvió una página web en vez del archivo.

        - Si es la página de ingreso: la sesión venció de verdad.
        - Si la página trae adentro el documento (visor), se baja ese.
        - Si no, se devuelve la página misma (.html) para no perder el contenido."""
        texto = html.decode("utf-8", "ignore")
        if ("sso.pjn.gov.ar" in url_final or 'id="kc-login"' in texto or 'name="password"' in texto
                or "openid-connect/auth" in texto):
            raise SesionVencida()
        if re.search(r"No posee permisos suficientes|No es posible acceder al documento", texto, re.I):
            raise SinPermiso()
        if profundidad == 0:
            from urllib.parse import urljoin
            enlaces = re.findall(r'(?:src|href|data)\s*=\s*["\']([^"\']+)["\']', texto, re.I)
            candidatos = [e for e in enlaces if re.search(r"(\.pdf\b|viewer\.seam|download|documento|archivo)",
                                                          e, re.I) and not e.lower().startswith(("javascript:", "#"))]
            for e in candidatos[:5]:
                destino = urljoin(url_final, e.replace("&amp;", "&"))
                try:
                    contenido, ext = self._bajar_una_vez(destino, segundos, profundidad=1)
                    if ext != ".html":
                        return contenido, ext
                except (SesionVencida, Cancelado, SinPermiso):
                    raise
                except Exception:
                    continue
        return html, ".html"

    def descargar_abierto(self, carpeta, reabrir):
        """Baja los documentos del expediente abierto, página por página.

        reabrir() vuelve a abrir este mismo expediente (se usa si el PJN deja de responder).
        Devuelve (documentos_en_el_expediente_vistos, errores)."""
        carpeta = Path(carpeta)
        carpeta.mkdir(parents=True, exist_ok=True)
        datos = self.datos_generales()
        (carpeta / "expediente.txt").write_text(
            "\n".join(f"{k}: {v}" for k, v in datos.items()) + "\n", encoding="utf-8")
        self.avisar(f"Leyendo {datos.get('Expediente', 'expediente')}…")

        est = _Estado(carpeta)
        ya_completo = est.completo
        est.completo = False
        cont = {"docs": 0, "nuevos": 0, "errores": 0, "sin_permiso": 0}
        ocurrencias = {}
        pendientes = []      # (clave, nombre_base, url) por bajar
        para_el_final = []   # documentos que el PJN no entregó a tiempo
        corte = {"listo": False}

        # Etapa 1: leer todas las páginas de corrido (sin bajar nada, así el PJN no corta la sesión).
        def procesar(origen, etiqueta):
            def _proc(filas, n):
                todas_conocidas = True
                for f in filas:
                    base = "|".join([f["fecha"], f["oficina"], f["tipo"], f["detalle"], f["fojas"]])
                    ocurrencias[base] = ocurrencias.get(base, 0) + 1
                    clave = f"{base}|#{ocurrencias[base]}"
                    if clave not in est.filas:
                        todas_conocidas = False
                        est.agregar_fila(clave, f, origen)
                    if not f["url"]:
                        continue
                    cont["docs"] += 1
                    if not est.tiene_archivo(clave):
                        todas_conocidas = False
                        pendientes.append((clave, limpiar_nombre(
                            f'{fecha_iso(f["fecha"])} {f["tipo"]} - {f["detalle"]}'), f["url"]))
                self.avisar(f"Leyendo {etiqueta.lower()}: página {n} · {len(pendientes)} documentos por bajar")
                if ya_completo and todas_conocidas:
                    corte["listo"] = True  # lo anterior ya estaba bajado completo
                    return False
                return True
            return _proc

        if self.page.locator(SEL_ACT).count():
            self._recorrer(SEL_ACT, SEL_ACT_SIG, "Actuaciones", procesar("actual", "Actuaciones"),
                           lambda k: self._ir_a_pagina_actuales(reabrir, k))
        if not corte["listo"]:
            if self._abrir_historicas():
                self._recorrer(SEL_HIST, SEL_HIST_SIG, "Actuaciones históricas",
                               procesar("histórica", "Actuaciones históricas"),
                               lambda k: self._ir_a_pagina_historicas(reabrir, k))
            else:
                self.avisar("Sin actuaciones históricas.")
        est.escribir_indice()

        # Etapa 2: bajar los archivos (los más viejos primero).
        pendientes.reverse()
        total = len(pendientes)
        self.avisar(f"{cont['docs']} documentos en la causa; {total} por bajar.", 0, total or 1)
        for i, (clave, nombre_base, url) in enumerate(pendientes, 1):
            self._chequear_cancelado()
            self.avisar(f"Bajando {i} de {total}: {nombre_base}", i - 1, total)
            try:
                contenido, ext = self._bajar(url)
            except SinPermiso:
                self.avisar(f"Sin permiso en el PJN para ver {nombre_base}: se anota y sigo.")
                est.marcar_sin_permiso(clave)
                cont["sin_permiso"] += 1
                continue
            except RuntimeError as e:
                self.avisar(f"Salteo por ahora {nombre_base} ({e}); lo reintento al final.")
                para_el_final.append((clave, nombre_base, url))
                continue
            if ext == ".html":
                if not self._sesion_vigente():
                    raise SesionVencida()
                self.avisar(f"El PJN muestra {nombre_base} como página web: se guarda así (.html).")
            est.guardar_archivo(clave, nombre_base, ext, contenido)
            cont["nuevos"] += 1
            if i % 10 == 0:
                est.escribir_indice()

        if para_el_final:
            self.avisar(f"Reintentando {len(para_el_final)} documento(s) que el PJN no entregó a tiempo…")
            for i, (clave, nombre_base, url) in enumerate(para_el_final, 1):
                self.avisar(f"Reintento {i} de {len(para_el_final)}: {nombre_base}", i - 1, len(para_el_final))
                try:
                    contenido, ext = self._bajar(url, intentos=2, segundos=90)
                except SinPermiso:
                    est.marcar_sin_permiso(clave)
                    cont["sin_permiso"] += 1
                    continue
                except RuntimeError as e:
                    self.avisar(f"No se pudo bajar {nombre_base}: {e}")
                    cont["errores"] += 1
                    continue
                if ext == ".html" and not self._sesion_vigente():
                    raise SesionVencida()
                est.guardar_archivo(clave, nombre_base, ext, contenido)
                cont["nuevos"] += 1
        if cont["errores"] == 0:
            est.completo = True
        est.escribir_indice()
        est.guardar()
        extra = (f" {cont['sin_permiso']} documento(s) el PJN no los deja ver con esta cuenta (figuran en el índice)."
                 if cont["sin_permiso"] else "")
        self.avisar(f"Listo: {cont['nuevos']} documentos nuevos ({est.cantidad_archivos()} en total en la carpeta)."
                    + extra, 1, 1)
        return est.cantidad_archivos(), cont["errores"]

    def descargar_expediente(self, numero, carpeta_base, con_vinculados=False, solo_consulta=False):
        """Abre y baja un expediente (y opcionalmente sus vinculados). Devuelve (carpeta, docs, errores)."""
        carpeta = Path(carpeta_base) / limpiar_nombre(normalizar_nro(numero).replace("/", "-"))
        self.avisar(f"Abriendo {numero}…")
        self.abrir_expediente(numero, solo_consulta)
        docs, errores = self.descargar_abierto(carpeta, lambda: self.abrir_expediente(numero, solo_consulta))
        if con_vinculados:
            self.abrir_expediente(numero, solo_consulta)
            lista = self.listar_vinculados()
            self.avisar(f"Vinculados: {len(lista)}")
            for k, v in enumerate(lista, 1):
                self._chequear_cancelado()
                self.avisar(f"Vinculado {k} de {len(lista)}: {v['expediente']}")
                sub = carpeta / "vinculados" / limpiar_nombre(normalizar_nro(v["expediente"]).replace("/", "-"))
                try:
                    self.abrir_vinculado(v, numero, solo_consulta)
                except (PWTimeout, RuntimeError) as e:
                    self.avisar(f"No se pudo abrir {v['expediente']}: {e}")
                    errores += 1
                    continue
                d, e = self.descargar_abierto(
                    sub, lambda v=v: self.abrir_vinculado(v, numero, solo_consulta))
                docs += d
                errores += e
        return carpeta, docs, errores


EXTENSIONES = {"application/pdf": ".pdf", "application/rtf": ".rtf", "text/rtf": ".rtf",
               "application/msword": ".doc", "image/jpeg": ".jpg", "image/png": ".png",
               "application/zip": ".zip", "image/tiff": ".tif"}


_SSL = None


def _contexto_ssl():
    """Certificados del sistema (los mismos que usa Edge); si no, los de certifi."""
    global _SSL
    if _SSL is None:
        import ssl
        try:
            import truststore
            _SSL = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            try:
                import certifi
                _SSL = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                _SSL = ssl.create_default_context()
    return _SSL


class _Estado:
    """Lo ya bajado de un expediente (.descargados.json), para seguir donde quedó."""

    def __init__(self, carpeta):
        self.carpeta = Path(carpeta)
        self.path = self.carpeta / ".descargados.json"
        datos = {}
        if self.path.exists():
            try:
                datos = json.loads(self.path.read_text("utf-8"))
            except Exception:
                datos = {}
        if datos and "version" not in datos:  # formato viejo: {clave: archivo}
            datos = {"version": 2, "archivos": datos, "filas": {}, "completo": False}
        self.archivos = datos.get("archivos", {})
        self.filas = datos.get("filas", {})
        self.completo = bool(datos.get("completo", False))
        self._orden = max([f.get("orden", 0) for f in self.filas.values()] + [0])
        self.encabezado = ["Fecha", "Origen", "Oficina", "Tipo", "Detalle", "A fs.", "Archivo"]

    def guardar(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 2, "completo": self.completo, "archivos": self.archivos,
                                   "filas": self.filas}, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    def agregar_fila(self, clave, f, origen):
        self._orden += 1
        self.filas[clave] = {"fecha": f["fecha"], "origen": origen, "oficina": f["oficina"], "tipo": f["tipo"],
                             "detalle": f["detalle"], "fojas": f["fojas"], "orden": self._orden}
        if origen == "adjunto" and f.get("url"):  # enlaces fijos de la MEV: sirven para reintentar después
            self.filas[clave]["url"] = f["url"]

    SIN_PERMISO = "(sin permiso en el PJN)"

    def tiene_archivo(self, clave):
        nombre = self.archivos.get(clave)
        if nombre == self.SIN_PERMISO:
            return True
        return bool(nombre) and (self.carpeta / nombre).exists()

    def marcar_sin_permiso(self, clave):
        self.archivos[clave] = self.SIN_PERMISO
        self.guardar()

    def guardar_archivo(self, clave, nombre_base, ext, contenido):
        usados = set(self.archivos.values())
        nombre, i = nombre_base + ext, 2
        while nombre in usados or (self.carpeta / nombre).exists():
            nombre = f"{nombre_base} ({i}){ext}"
            i += 1
        (self.carpeta / nombre).write_bytes(contenido)
        self.archivos[clave] = nombre
        self.guardar()

    def cantidad_archivos(self):
        return sum(1 for n in self.archivos.values() if n != self.SIN_PERMISO and (self.carpeta / n).exists())

    def escribir_indice(self):
        filas = sorted(self.filas.items(), key=lambda kv: (fecha_iso(kv[1]["fecha"]), -kv[1].get("orden", 0)))
        try:
            with open(self.carpeta / "indice.csv", "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh, delimiter=";")
                w.writerow(self.encabezado)
                for clave, f in filas:
                    w.writerow([f["fecha"], f["origen"], f["oficina"], f["tipo"], f["detalle"], f["fojas"],
                                self.archivos.get(clave, "")])
        except PermissionError:
            pass  # el índice está abierto en Excel: se actualiza la próxima vez
        self.guardar()
