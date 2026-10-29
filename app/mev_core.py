"""
Acceso a la Mesa de Entradas Virtual (MEV) de la Suprema Corte de Buenos Aires.

Ingreso, elección de jurisdicción y organismo, búsqueda de causas y descarga de todos los
pasos procesales (cada uno como PDF con su texto) y sus adjuntos. Usa el mismo Playwright
que la parte del PJN y el mismo hilo.
"""

import re
import time
import urllib.request
from pathlib import Path

import pjn_core
from pjn_core import Cancelado, SinPermiso, _Estado, _contexto_ssl, limpiar_nombre, USER_AGENT

MEV = "https://mev.scba.gov.ar"

# "Creado en" de la pantalla de ingreso (departamento donde se registró el usuario).
CREADO_EN = [
    ("aa", "- Todos los departamentos"), ("AZ", "Azul"), ("AZF", "Azul - Familia"), ("BB", "Bahía Blanca"),
    ("BBF", "Bahía Blanca - Familia"), ("DO", "Dolores"), ("DOF", "Dolores - Familia"), ("JU", "Junín"),
    ("JUF", "Junín - Familia"), ("LM", "La Matanza"), ("LMF", "La Matanza - Familia"), ("LP", "La Plata"),
    ("LPF", "La Plata - Familia"), ("LZ", "Lomas de Zamora"), ("LZF", "Lomas de Zamora - Familia"),
    ("MP", "Mar del Plata"), ("MPF", "Mar del Plata - Familia"), ("ME", "Mercedes"), ("MEF", "Mercedes - Familia"),
    ("MR", "Moreno - Gral. Rodríguez"), ("MRF", "Moreno - Gral. Rodríguez - Familia"), ("MO", "Morón"),
    ("MOF", "Morón - Familia"), ("NE", "Necochea"), ("NEF", "Necochea - Familia"), ("OL", "Olavarría"),
    ("OLF", "Olavarría - Familia"), ("PE", "Pergamino"), ("PEF", "Pergamino - Familia"), ("QU", "Quilmes"),
    ("QUF", "Quilmes - Familia"), ("SI", "San Isidro"), ("SIF", "San Isidro - Familia"), ("SM", "San Martín"),
    ("SMF", "San Martín - Familia"), ("SN", "San Nicolás"), ("SNF", "San Nicolás - Familia"), ("TN", "Tandil"),
    ("TNF", "Tandil - Familia"), ("TL", "Trenque Lauquen"),
]

# Dónde buscar: departamentos judiciales (código del PJBA), Suprema Corte y Justicia de Paz.
JURISDICCIONES = [
    ("80", "Avellaneda-Lanús"), ("10", "Azul"), ("11", "Bahía Blanca"), ("12", "Dolores"), ("13", "Junín"),
    ("14", "La Matanza"), ("6", "La Plata"), ("16", "Lomas de Zamora"), ("17", "Mar del Plata"),
    ("18", "Mercedes"), ("52", "Moreno - Gral. Rodríguez"), ("19", "Morón"), ("20", "Necochea"),
    ("21", "Olavarría"), ("22", "Pergamino"), ("23", "Quilmes"), ("24", "San Isidro"), ("25", "San Martín"),
    ("26", "San Nicolás"), ("27", "Tandil"), ("28", "Trenque Lauquen"), ("49", "Tres Arroyos"),
    ("29", "Zárate - Campana"), ("SCJ", "Suprema Corte de Justicia"), ("PZ", "Justicia de Paz"),
]

MODOS = {"numero": "xNc", "caratula": "xCa", "receptoria": "xNr"}
CAMPO_MODO = {"numero": "NCausa", "caratula": "caratula", "receptoria": "NInterno"}
ESTADOS = {"activas": "Ac", "archivadas": "Ar", "ambas": "Am"}


class ErrorLoginMEV(Exception):
    pass


class SesionVencidaMEV(Exception):
    pass


JS_RESULTADOS = r"""
() => {
  // Cada causa tiene una fila con su casilla "Marcados" y el enlace de la carátula; la fila
  // siguiente trae estado, receptoría, expediente, inicio y último paso.
  const out = [];
  const limpiar = s => (s || '').replace(/\s+/g, ' ').trim();
  for (const marca of document.querySelectorAll('input[name="Marcados"]')) {
    const fila = marca.closest('tr');
    const a = fila && fila.querySelector('a[href*="procesales.asp"]');
    if (!a) continue;
    const u = new URL(a.href);
    const det = fila.nextElementSibling;
    const celdas = det ? [...det.cells].map(c => limpiar(c.innerText)) : [];
    out.push({caratula: limpiar(a.innerText).replace(/\s*-\s*$/, ''),
              nid: u.searchParams.get('nidCausa'), pid: u.searchParams.get('pidJuzgado'),
              estado: celdas[0] || '', receptoria: celdas[1] || '', expediente: celdas[2] || '',
              inicio: celdas[3] || '', ultimo: celdas[4] || ''});
  }
  return out;
}
"""

JS_PASOS = r"""
() => {
  const limpiar = s => (s || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('a[href*="proveido.asp"]')].map(a => {
    const fila = a.closest('tr');
    const c = fila ? [...fila.cells].map(x => limpiar(x.innerText)) : [];
    const u = new URL(a.href);
    return {fecha_hora: c[0] || '', fojas: c[1] || '', descripcion: limpiar(a.innerText),
            nposi: u.searchParams.get('nPosi'), url: a.href};
  });
}
"""

JS_DATOS = r"""
() => {
  const t = document.body.innerText;
  const campo = n => { const m = t.match(new RegExp(n + ':\\s*([^\\n\\t]+)')); return m ? m[1].trim() : ''; };
  return {caratula: campo('Carátula'), inicio: campo('Fecha inicio'), receptoria: campo('Nº de Receptoría'),
          expediente: campo('Nº de Expediente'), estado: campo('Estado')};
}
"""

# Texto del paso listo para imprimir: se sacan la lista desplegable y los botones de navegación.
JS_PASO = r"""
() => {
  const div = document.getElementById('imprime');
  if (!div) return null;
  const k = div.cloneNode(true);
  k.querySelectorAll('select, script, img, input, button').forEach(e => e.remove());
  k.querySelectorAll('a').forEach(a => {
    const t = a.innerText.trim();
    if (/^(Anterior|Siguiente|Imprimir|Volver)/i.test(t)) a.remove();
  });
  const adjuntos = [...div.querySelectorAll('a')].filter(a => /VER ADJUNTO/i.test(a.innerText)).map(a => a.href);
  return {html: k.innerHTML, adjuntos};
}
"""

PLANTILLA = """<html><head><meta charset="utf-8"><style>
body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10.5pt; color: #111; margin: 0; }}
table {{ width: 100%; border-collapse: collapse; }} td {{ vertical-align: top; padding: 1px 4px; }}
a {{ color: #111; text-decoration: none; }} .cab {{ border-bottom: 2px solid #333; margin-bottom: 8px; padding-bottom: 4px; }}
</style></head><body><div class="cab"><b>{titulo}</b><br>{subtitulo}</div>{cuerpo}</body></html>"""


class MEVSesion:
    def __init__(self, pw, avisar=None, cancelar=None):
        self._pw = pw
        self.avisar = avisar or (lambda texto, actual=None, total=None: print(texto, flush=True))
        self.cancelar = cancelar
        self.browser = self.ctx = self.page = self._pdf = None
        self.jurisdiccion = None
        self.credenciales = None

    # --- ciclo de vida

    def abrir(self):
        import sys
        browser = None
        if sys.platform == "win32":
            try:
                browser = self._pw.chromium.launch(channel="msedge", headless=True)
            except Exception:
                browser = None
        self.browser = browser or self._pw.chromium.launch(headless=True)
        self.ctx = self.browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900},
                                            locale="es-AR")
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(90000)
        self._pdf = self.ctx.new_page()  # para armar los PDF de cada paso

    def cerrar(self):
        for cosa in (self.ctx, self.browser):
            try:
                if cosa:
                    cosa.close()
            except Exception:
                pass
        self.browser = self.ctx = self.page = self._pdf = None

    def _chequear_cancelado(self):
        if self.cancelar is not None and self.cancelar.is_set():
            raise Cancelado()

    def _chequear_sesion(self):
        if "loguin.asp" in self.page.url.lower() and "pos" not in self.page.url.lower():
            raise SesionVencidaMEV()

    # --- ingreso

    def ingresar(self, usuario, clave, creado_en):
        if self.page is None:
            self.abrir()
        page = self.page
        page.goto(f"{MEV}/loguin.asp", wait_until="domcontentloaded")
        page.fill("input[name=usuario]", usuario)
        page.fill("input[name=clave]", clave)
        try:
            page.select_option("select[name=DeptoRegistrado]", creado_en)
        except Exception:
            pass
        page.locator("form[name=frmLoguin] input[type=submit]").first.click()
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1)
        if "posloguin" in page.url.lower() or page.locator("select[name=DtoJudElegido]").count():
            self.credenciales = (usuario, clave, creado_en)
            self.jurisdiccion = None
            return
        texto = re.sub(r"\s+", " ", page.locator("body").inner_text())
        print(f"[diagnóstico MEV] el ingreso no pasó: {page.url.split('?')[0]} | {texto[:400]!r}", flush=True)
        m = re.search(r"(usuario[^.]{0,80}(incorrect|inv[aá]lid|no existe)[^.]*|contrase[ñn]a[^.]{0,80}"
                      r"(incorrect|inv[aá]lid|vencid|caduc)[^.]*|clave[^.]{0,80}(incorrect|inv[aá]lid|vencid)[^.]*)",
                      texto, re.I)
        raise ErrorLoginMEV(m.group(0).strip() if m else "Usuario, clave o «Creado en» incorrectos.")

    def reingresar(self):
        if not self.credenciales:
            raise SesionVencidaMEV()
        self.avisar("La sesión de la MEV venció; ingresando de nuevo…")
        self.ingresar(*self.credenciales)

    # --- jurisdicción y organismos

    def elegir_jurisdiccion(self, codigo):
        """Elige departamento / Suprema Corte / Paz. Devuelve la lista de organismos [(codigo, nombre)]."""
        page = self.page
        page.goto(f"{MEV}/POSLoguin.asp", wait_until="domcontentloaded")
        self._chequear_sesion()
        if codigo in ("SCJ", "PZ", "LPC"):
            page.check(f"input[name=TipoDto][value={codigo}]")
        else:
            page.check("input[name=TipoDto][value=CC]")
            page.select_option("select[name=DtoJudElegido]", codigo)
        page.click("input[name=Aceptar]")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(0.5)
        self._chequear_sesion()
        if re.search(r"NO AUTORIZADO", page.locator("body").inner_text(), re.I):
            raise RuntimeError("La MEV no autoriza a este usuario a consultar esa jurisdicción.")
        self.jurisdiccion = codigo
        return page.evaluate("""() => [...(document.querySelector('select[name=JuzgadoElegido]') || {options: []}).options]
                                 .map(o => [o.value.trim(), o.text.trim()]).filter(x => x[0])""")

    # --- búsqueda

    def buscar(self, jurisdiccion, organismo, modo, texto, estado="ambas"):
        if self.jurisdiccion != jurisdiccion or "busqueda" not in self.page.url.lower():
            self.elegir_jurisdiccion(jurisdiccion)
        page = self.page
        if "busqueda.asp" not in page.url.lower():
            page.goto(f"{MEV}/busqueda.asp", wait_until="domcontentloaded")
        self._chequear_sesion()
        ok = page.evaluate("""(org) => { const s = document.querySelector('select[name=JuzgadoElegido]');
                                const o = s && [...s.options].find(o => o.value.trim() === org);
                                if (!o) return false; s.value = o.value; return true; }""", organismo)
        if not ok:
            raise RuntimeError("No encontré ese juzgado en la MEV.")
        page.check(f"input[name=radio][value={MODOS[modo]}]")
        page.fill(f"input[name={CAMPO_MODO[modo]}]", texto)
        page.check(f"input[name=TipoCausa][value={ESTADOS.get(estado, 'Am')}]")
        page.click("input[name=Buscar]")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(0.5)
        self._chequear_sesion()
        cuerpo = page.locator("body").inner_text()
        m = re.search(r"Los resultados exceden el l[ií]mite[^\n]*", cuerpo)
        if m:
            raise RuntimeError("Hay demasiados resultados: escriba más datos (por ejemplo, nombre y apellido).")
        return page.evaluate(JS_RESULTADOS)

    # --- descarga

    def _bajar(self, url, segundos=60, intentos=2):
        ultimo = None
        for intento in range(intentos):
            self._chequear_cancelado()
            try:
                cookies = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies(url))
                req = urllib.request.Request(url, headers={"Cookie": cookies, "User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=segundos, context=_contexto_ssl()) as r:
                    ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    datos = []
                    while True:
                        self._chequear_cancelado()
                        b = r.read(256 * 1024)
                        if not b:
                            break
                        datos.append(b)
                    return b"".join(datos), ct
            except Cancelado:
                raise
            except Exception as e:
                ultimo = "la MEV dejó de enviar datos" if "timed out" in str(e) else str(e)
            time.sleep(3 * (intento + 1))
        raise RuntimeError(ultimo)

    def _leer_paso(self, url):
        """Abre un paso y devuelve {html, adjuntos}; la MEV a veces no lo muestra: se reintenta."""
        for intento in range(3):
            self._chequear_cancelado()
            self.page.goto(url, wait_until="domcontentloaded")
            self._chequear_sesion()
            paso = self.page.evaluate(JS_PASO)
            if paso:
                return paso
            texto = self.page.locator("body").inner_text()
            if re.search(r"no (est[aá]|se encuentra) autorizad|no tiene permiso|acceso denegado", texto, re.I):
                raise SinPermiso()
            time.sleep(2 + 3 * intento)
        raise RuntimeError("la MEV no mostró el texto del paso")

    def _bajar_adjunto(self, est, clave_adj, base, url, nombre):
        contenido, ct = self._bajar(url)
        ext = Path(nombre).suffix.lower() or (".pdf" if "pdf" in ct else ".bin")
        est.guardar_archivo(clave_adj, limpiar_nombre(f"{base} - adjunto {Path(nombre).stem}"), ext, contenido)

    def _paso_a_pdf(self, html, titulo, subtitulo):
        self._pdf.set_content(PLANTILLA.format(titulo=titulo, subtitulo=subtitulo, cuerpo=html),
                              wait_until="load")
        return self._pdf.pdf(format="A4", print_background=True,
                             margin={"top": "16mm", "bottom": "16mm", "left": "15mm", "right": "15mm"})

    def descargar_causa(self, nid, pid, carpeta):
        """Baja todos los pasos procesales (PDF) y adjuntos de la causa. Devuelve (carpeta, docs, errores)."""
        page = self.page
        carpeta = Path(carpeta)
        carpeta.mkdir(parents=True, exist_ok=True)
        page.goto(f"{MEV}/procesales.asp?nidCausa={nid}&pidJuzgado={pid}", wait_until="domcontentloaded")
        self._chequear_sesion()
        datos = page.evaluate(JS_DATOS)
        (carpeta / "expediente.txt").write_text(
            "\n".join(f"{k.capitalize()}: {v}" for k, v in datos.items() if v) + "\n", encoding="utf-8")
        pasos = page.evaluate(JS_PASOS)
        self.avisar(f"{datos.get('caratula') or 'Causa'}: {len(pasos)} pasos procesales.")

        est = _Estado(carpeta)
        est.encabezado = ["Fecha", "Tipo", "Hora", "Trámite", "Detalle", "Fojas", "Archivo"]
        pendientes = []
        for p in pasos:
            fecha = p["fecha_hora"].split(" ")[0]
            clave = f"paso|{p['nposi']}"
            if clave not in est.filas:
                est.agregar_fila(clave, {"fecha": fecha, "oficina": p["fecha_hora"][len(fecha):].strip(),
                                         "tipo": p["descripcion"], "detalle": "", "fojas": p["fojas"],
                                         "url": p["url"]}, "paso")
            if not est.tiene_archivo(clave):
                pendientes.append((clave, fecha, p))
        est.escribir_indice()
        pendientes.reverse()  # los más viejos primero
        total, nuevos, errores, sin_permiso = len(pendientes), 0, 0, 0
        titulo = datos.get("caratula", "")
        fallidos = []

        def bajar_paso(clave, fecha, p):
            nonlocal nuevos, sin_permiso
            base = limpiar_nombre(f"{pjn_core.fecha_iso(fecha)} {p['descripcion']}")
            try:
                paso = self._leer_paso(p["url"])
            except SinPermiso:
                est.marcar_sin_permiso(clave)
                sin_permiso += 1
                return True
            pdf = self._paso_a_pdf(paso["html"], titulo,
                                   f"{datos.get('expediente', '')} · {p['fecha_hora']} · {p['descripcion']}")
            est.guardar_archivo(clave, base, ".pdf", pdf)
            nuevos += 1
            for j, url in enumerate(paso["adjuntos"], 1):
                clave_adj = f"adj|{p['nposi']}|{j}"
                if est.tiene_archivo(clave_adj):
                    continue
                m = re.search(r"nombrepath=([^&]+)", url)
                nombre = urllib.request.unquote(m.group(1).replace("+", " ")) if m else f"adjunto {j}"
                est.agregar_fila(clave_adj, {"fecha": fecha, "oficina": "", "tipo": p["descripcion"],
                                             "detalle": f"Adjunto: {nombre}", "fojas": "", "url": url}, "adjunto")
                est.filas[clave_adj]["base"] = base
                est.filas[clave_adj]["nombre"] = nombre
                try:
                    self._bajar_adjunto(est, clave_adj, base, url, nombre)
                    nuevos += 1
                except RuntimeError as e:
                    self.avisar(f"Adjunto {nombre}: {e}; lo reintento al final.")
            return True

        for i, (clave, fecha, p) in enumerate(pendientes, 1):
            self._chequear_cancelado()
            self.avisar(f"Bajando {i} de {total}: {pjn_core.fecha_iso(fecha)} {p['descripcion']}", i - 1, total)
            try:
                bajar_paso(clave, fecha, p)
            except (SesionVencidaMEV, Cancelado):
                raise
            except Exception as e:
                self.avisar(f"Salteo por ahora {p['descripcion']} ({e}); lo reintento al final.")
                fallidos.append((clave, fecha, p))
            time.sleep(0.4)  # para no saturar a la MEV
            if i % 10 == 0:
                est.escribir_indice()

        if fallidos:
            self.avisar(f"Reintentando {len(fallidos)} paso(s)…")
            time.sleep(5)
            for clave, fecha, p in fallidos:
                try:
                    bajar_paso(clave, fecha, p)
                except (SesionVencidaMEV, Cancelado):
                    raise
                except Exception as e:
                    self.avisar(f"No se pudo bajar {p['descripcion']} ({fecha}): {e}")
                    errores += 1
        # Adjuntos que quedaron pendientes (de esta vez o de una descarga anterior).
        for clave_adj, fila in list(est.filas.items()):
            if clave_adj.startswith("adj|") and fila.get("url") and not est.tiene_archivo(clave_adj):
                try:
                    self._bajar_adjunto(est, clave_adj, fila.get("base", fila["tipo"]), fila["url"],
                                        fila.get("nombre", "adjunto"))
                    nuevos += 1
                except Exception as e:
                    self.avisar(f"No se pudo bajar el adjunto {fila.get('nombre', '')}: {e}")
                    errores += 1
        est.completo = errores == 0
        est.escribir_indice()
        extra = f" {sin_permiso} paso(s) sin autorización en la MEV." if sin_permiso else ""
        self.avisar(f"Listo: {nuevos} archivos nuevos ({est.cantidad_archivos()} en total en la carpeta).{extra}",
                    1, 1)
        return carpeta, est.cantidad_archivos(), errores
