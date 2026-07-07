"""
Genera data.json para el KAM Dashboard leyendo directamente desde Google Sheets.

Fuentes (2 spreadsheets):

  Spreadsheet principal (SPREADSHEET_ID):
    - Tickets   -> transacciones. Columna "empresa" = ID Empresa (clave de join).
    - Usuarios  -> columna "N° usuarios incentivados" (col L) por ID Empresa.
    - Reuniones -> reuniones por ejecutivo (join por KAM ID / email contra Leyenda).
    - Leyenda   -> Email -> {rol, pais}. También define el headcount por rol/país
                   (se cuenta a todos los que aparecen ahí, tengan o no cuentas asignadas).

  Spreadsheet "Empresas (Todos los países)" (EMPRESAS_SPREADSHEET_ID):
    - Una pestaña por país (Chile, Peru, Colombia, Ecuador, Mexico), cada una con
      columnas ID Empresa / Rut / Kam / Email Kam. Es el mapa cuenta -> ejecutivo dueño.

Con esto, cada ticket y cada fila de usuarios se atribuye a un ejecutivo real
(vía ID Empresa -> Email Kam -> Leyenda), en vez de repartir el total del país
por igual entre todos los roles.

Corre en GitHub Actions con una service account (ver GOOGLE_SHEETS_CREDENTIALS).
"""

import os
import re
import json
import hashlib
import unicodedata
from datetime import datetime, timezone
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "19kyAlO7_3vF1BZnRDw0Yi5A0QN4w80akeHbXnUMXgKM"
EMPRESAS_SPREADSHEET_ID = "1DvVU7NHLh-EraghQm5Qc9RNLKSVfsY5MJDfL0WJGhEw"
EMPRESAS_TABS = ["Chile", "Peru", "Colombia", "Ecuador", "Mexico"]

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")
UNMATCHED_REPORT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sin_asignar.json")

COUNTRY_ORDER = ["Chile", "Colombia", "Ecuador", "México", "Perú"]
COUNTRY_FLAGS = {
    "Chile": "🇨🇱",
    "Colombia": "🇨🇴",
    "Ecuador": "🇪🇨",
    "México": "🇲🇽",
    "Perú": "🇵🇪",
}
ROLES = ["KAM", "BDM", "Full Cycle"]

MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

COUNTRY_ALIASES = {
    "chile": "Chile",
    "colombia": "Colombia",
    "ecuador": "Ecuador",
    "mexico": "México",
    "méxico": "México",
    "peru": "Perú",
    "perú": "Perú",
}

# Mientras la Leyenda / Empresas no estén 100% completas, los tickets y usuarios que
# no logran matchear con ningún ejecutivo real se reparten -en vez de quedar excluidos
# de los promedios- entre los roles indicados acá, según estas proporciones.
# El reparto es determinístico (por ID Empresa), no al azar, para que los números no
# salten entre corridas del pipeline. País sin entrada acá = se sigue excluyendo (Ecuador).
FALLBACK_SPLIT = {
    "Chile": [("KAM", 1.0)],
    "Colombia": [("KAM", 0.9), ("BDM", 0.1)],
    "México": [("Full Cycle", 1.0)],
    "Perú": [("KAM", 1.0)],
}


# ---------------------------------------------------------------------------
# Helpers genéricos
# ---------------------------------------------------------------------------

def normalize_key(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def build_header_index(headers):
    idx = {}
    for h in headers:
        idx[normalize_key(h)] = h
    return idx


def find_header(header_index, *candidates):
    for c in candidates:
        nc = normalize_key(c)
        if nc in header_index:
            return header_index[nc]
    for c in candidates:
        nc = normalize_key(c)
        for k, orig in header_index.items():
            if nc and (nc in k or k in nc):
                return orig
    return None


def get_records(ws):
    """Lee una worksheet como lista de dicts, tolerando headers duplicados/vacíos."""
    values = ws.get_all_values()
    if not values:
        return [], {}
    headers = values[0]
    seen = {}
    clean_headers = []
    for i, h in enumerate(headers):
        h = h.strip() if h else f"col_{i}"
        if h in seen:
            seen[h] += 1
            h = f"{h}_{seen[h]}"
        else:
            seen[h] = 0
        clean_headers.append(h)
    header_index = build_header_index(clean_headers)
    records = []
    for row in values[1:]:
        if not any(cell.strip() for cell in row):
            continue
        row = row + [""] * (len(clean_headers) - len(row))
        records.append(dict(zip(clean_headers, row)))
    return records, header_index


def normalize_country(raw):
    if not raw:
        return None
    raw = raw.strip()
    return COUNTRY_ALIASES.get(raw.lower(), raw)


def normalize_tax_id(raw):
    if not raw:
        return None
    return re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()


def normalize_empresa_id(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    if s.endswith(".0"):
        s = s[:-2]
    return s


def parse_number(raw):
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s == "" or s.upper() in ("N/A", "NA", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def month_key(year, month):
    return f"{int(year):04d}-{int(month):02d}"


def month_label(mk):
    y, m = mk.split("-")
    return f"{MESES[int(m) - 1]} {y[2:]}"


def safe_div(a, b):
    return round(a / b, 2) if b else 0


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_client():
    creds_raw = os.environ["GOOGLE_SHEETS_CREDENTIALS"]
    creds_dict = json.loads(creds_raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def build_empresa_owner_map(gc):
    """Lee la spreadsheet 'Empresas (Todos los países)' y arma (país, ID Empresa) -> email dueño.

    IMPORTANTE: la clave incluye el país porque el "ID Empresa" es correlativo por
    pestaña/país (cada país arranca su propia numeración), NO es un ID global. Si se
    indexara solo por ID (sin país), un ID "150" de Chile y un ID "150" de Colombia
    -empresas totalmente distintas- se pisarían entre sí en el diccionario, mezclando
    tickets/usuarios de un país con el ejecutivo equivocado de otro país.
    """
    sh = gc.open_by_key(EMPRESAS_SPREADSHEET_ID)
    owner_map = {}
    ids_vistos = defaultdict(set)  # eid -> {paises donde aparece} (para detectar colisiones)
    for tab in EMPRESAS_TABS:
        pais = normalize_country(tab)
        try:
            ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            continue
        records, h_idx = get_records(ws)
        col_id = find_header(h_idx, "ID Empresa")
        col_email = find_header(h_idx, "Email Kam")
        for r in records:
            eid = normalize_empresa_id(r.get(col_id, ""))
            email = (r.get(col_email, "") or "").strip().lower()
            if not eid or not email:
                continue
            owner_map[(pais, eid)] = {"email": email, "pais": pais}
            ids_vistos[eid].add(pais)

    colisiones = {eid: sorted(paises) for eid, paises in ids_vistos.items() if len(paises) > 1}
    if colisiones:
        print(
            f"AVISO: {len(colisiones)} ID Empresa se repiten en más de un país "
            f"(ej. {dict(list(colisiones.items())[:5])}). Cada uno se resuelve por su "
            f"propio país, no se mezclan entre sí."
        )
    return owner_map


# ---------------------------------------------------------------------------
# Lógica pura de agregación
# ---------------------------------------------------------------------------

def build_dashboard_data(
    tickets_records, tickets_h,
    usuarios_records, usuarios_h,
    reuniones_records, reuniones_h,
    leyenda_records, leyenda_h,
    empresa_owner_map,
):
    # --- Leyenda: KAM ID / correo -> {rol, pais}; también headcount por rol/pais ----
    col_correo = find_header(leyenda_h, "Correo", "Email")
    col_kamid = find_header(leyenda_h, "KAM ID")
    col_rol = find_header(leyenda_h, "Rol")
    col_pais_ley = find_header(leyenda_h, "Pais", "País")
    col_nombre_ley = find_header(leyenda_h, "Nombre", "Nombre Ejecutivo", "Ejecutivo", "Nombre completo")

    def display_name(correo, raw_nombre):
        nombre = (raw_nombre or "").strip()
        if nombre:
            return nombre
        # Sin columna de nombre (o vacía): derivamos algo legible del correo.
        local = correo.split("@")[0].replace(".", " ").replace("_", " ")
        return " ".join(p.capitalize() for p in local.split() if p) or correo

    kamid_to_info = {}
    kamid_to_correo = {}
    email_to_info = {}
    ejecutivos_set = defaultdict(lambda: defaultdict(set))  # ejecutivos_set[rol][pais] = {emails}
    correo_to_exec = {}  # correo -> {"nombre", "rol", "pais"}

    for r in leyenda_records:
        pais = normalize_country(r.get(col_pais_ley, ""))
        rol = (r.get(col_rol, "") or "").strip()
        kamid = (r.get(col_kamid, "") or "").strip()
        correo = (r.get(col_correo, "") or "").strip().lower()
        if not pais or not rol:
            continue
        info = {"rol": rol, "pais": pais}
        if kamid and kamid.upper() != "#N/A":
            kamid_to_info[kamid] = info
            if correo:
                kamid_to_correo[kamid] = correo
        if correo:
            email_to_info[correo] = info
            ejecutivos_set[rol][pais].add(correo)
            correo_to_exec[correo] = {
                "nombre": display_name(correo, r.get(col_nombre_ley, "")),
                "rol": rol,
                "pais": pais,
            }

    ejecutivos = {rol: {pais: len(emails) for pais, emails in paises.items()} for rol, paises in ejecutivos_set.items()}

    def resolve_owner(empresa_id, pais_fila):
        """Dado un ID Empresa y el país de la fila (Tickets/Usuarios), intenta resolver
        el ejecutivo dueño (rol/pais). El "ID Empresa" es correlativo POR PAÍS en la hoja
        Empresas (cada país tiene su propia numeración), así que hay que buscarlo en la
        pestaña de ESE país -no en un pool global-, para no mezclar empresas de países
        distintos que comparten el mismo ID por coincidencia.
        Devuelve dict con rol/pais si matchea, o motivo + email (si corresponde) si no."""
        eid = normalize_empresa_id(empresa_id)
        if not eid:
            return {"rol": None, "pais": None, "motivo": "sin_id_empresa", "owner_email": None}
        if not pais_fila:
            return {"rol": None, "pais": None, "motivo": "sin_pais_en_fila", "owner_email": None}
        owner = empresa_owner_map.get((pais_fila, eid))
        if not owner:
            return {"rol": None, "pais": None, "motivo": "empresa_no_esta_en_hoja_empresas", "owner_email": None}
        info = email_to_info.get(owner["email"])
        if not info:
            return {"rol": None, "pais": None, "motivo": "email_kam_no_esta_en_leyenda", "owner_email": owner["email"]}
        return {"rol": info["rol"], "pais": info["pais"], "motivo": None, "owner_email": owner["email"]}

    def pick_fallback_rol(pais, seed_key):
        """Si el país tiene una regla en FALLBACK_SPLIT, elige un rol respetando esas
        proporciones (reparto determinístico según seed_key, no al azar). Devuelve None
        si el país no tiene regla definida (se sigue excluyendo, como antes)."""
        split = FALLBACK_SPLIT.get(pais)
        if not split:
            return None
        digest = hashlib.md5(str(seed_key).encode("utf-8")).hexdigest()
        r = int(digest[:8], 16) / 0xFFFFFFFF
        acumulado = 0.0
        for rol, peso in split:
            acumulado += peso
            if r < acumulado:
                return rol
        return split[-1][0]

    # --- Tickets: atribuir cada ticket a (rol, pais) vía ID Empresa -------------
    col_pais_tix = find_header(tickets_h, "pais", "país")
    col_empresa_id = find_header(tickets_h, "empresa")
    col_idtrib = find_header(tickets_h, "id_tributario")
    col_fecha = find_header(tickets_h, "fechaCreacion")
    col_nombre_empresa_tix = find_header(tickets_h, "nombre empresa")

    tickets_by_role_country_month = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    clientes_by_role_country = defaultdict(lambda: defaultdict(set))
    tickets_sin_asignar = 0
    tickets_redistribuidos = 0
    tickets_sin_asignar_detalle = {}  # key: empresa_id -> {..., count}

    # Detalle por ejecutivo real (solo cuentas SÍ atribuidas a una persona concreta)
    tickets_by_exec_month = defaultdict(lambda: defaultdict(int))  # [correo][mk] = count
    clientes_by_exec = defaultdict(set)  # [correo] = {RUTs}

    # Pool de lo redistribuido (no atribuible a una persona, repartido por rol/país)
    tickets_pool_by_role_country_month = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    clientes_pool_by_role_country = defaultdict(lambda: defaultdict(set))

    for r in tickets_records:
        pais_ticket = normalize_country(r.get(col_pais_tix, ""))
        if not pais_ticket:
            continue
        empresa_id = r.get(col_empresa_id, "")
        res = resolve_owner(empresa_id, pais_ticket)
        rol = res["rol"]
        es_fallback = False
        if rol:
            # Ya identificamos al dueño real: confiamos en SU país registrado en la Leyenda
            # por sobre la columna "pais" de la fila del ticket (que puede venir mal tipeada).
            # Así el total del país y el detalle por ejecutivo siempre usan el mismo país.
            pais_bucket = res["pais"]
        else:
            tickets_sin_asignar += 1
            key = normalize_empresa_id(empresa_id) or f"SIN_ID::{r.get(col_idtrib, '')}"
            pais_bucket = pais_ticket
            rol = pick_fallback_rol(pais_bucket, key)
            es_fallback = True
            if rol:
                tickets_redistribuidos += 1
            entry = tickets_sin_asignar_detalle.setdefault(key, {
                "empresa_id": normalize_empresa_id(empresa_id),
                "nombre_empresa": (r.get(col_nombre_empresa_tix, "") or "").strip(),
                "rut": r.get(col_idtrib, ""),
                "pais": pais_ticket,
                "motivo": res["motivo"],
                "email_kam_en_empresas": res["owner_email"],
                "redistribuido_a": rol,
                "count": 0,
            })
            entry["count"] += 1
            if not rol:
                continue  # país sin regla de reparto -> se sigue excluyendo, como antes

        id_trib = normalize_tax_id(r.get(col_idtrib, ""))
        if id_trib:
            clientes_by_role_country[rol][pais_bucket].add(id_trib)
            if es_fallback:
                clientes_pool_by_role_country[rol][pais_bucket].add(id_trib)
            else:
                clientes_by_exec[res["owner_email"]].add(id_trib)

        fecha = (r.get(col_fecha, "") or "").strip()
        if len(fecha) >= 10:
            try:
                dt = datetime.strptime(fecha[:10], "%Y-%m-%d")
                mk = month_key(dt.year, dt.month)
                tickets_by_role_country_month[rol][pais_bucket][mk] += 1
                if es_fallback:
                    tickets_pool_by_role_country_month[rol][pais_bucket][mk] += 1
                else:
                    tickets_by_exec_month[res["owner_email"]][mk] += 1
            except ValueError:
                pass

    tickets_mensual = defaultdict(dict)
    tickets_n_meses = defaultdict(dict)  # meses activos usados como denominador (compartido con exec/pool)
    for rol in ROLES:
        for pais in COUNTRY_ORDER:
            months = tickets_by_role_country_month[rol].get(pais, {})
            active = [v for v in months.values() if v > 0]
            tickets_mensual[rol][pais] = round(sum(active) / len(active), 2) if active else 0
            tickets_n_meses[rol][pais] = len(active)

    total_clientes = defaultdict(dict)
    for rol in ROLES:
        for pais in COUNTRY_ORDER:
            total_clientes[rol][pais] = len(clientes_by_role_country[rol].get(pais, set()))

    # --- Usuarios: atribuir columna L (usuarios incentivados) vía ID Empresa ----
    col_usuarios_l = find_header(usuarios_h, "N usuarios incentivados", "usuarios incentivados")
    col_pais_usr = find_header(usuarios_h, "Pais", "País")
    col_id_empresa_usr = find_header(usuarios_h, "ID Empresa")
    col_nombre_empresa_usr = find_header(usuarios_h, "Nombre Empresa")
    col_idtrib_usr = find_header(usuarios_h, "ID Tributario")

    usuarios_total = defaultdict(lambda: defaultdict(float))
    usuarios_sin_asignar = 0
    usuarios_redistribuidos = 0
    usuarios_sin_asignar_detalle = {}

    usuarios_by_exec = defaultdict(float)  # [correo] = total
    usuarios_pool_by_role_country = defaultdict(lambda: defaultdict(float))

    for r in usuarios_records:
        l_val = parse_number(r.get(col_usuarios_l, ""))
        if l_val is None:
            continue  # columna L vacía => empresa sin compras hace +12 meses, se excluye
        pais_usr = normalize_country(r.get(col_pais_usr, ""))
        if not pais_usr:
            continue
        empresa_id = r.get(col_id_empresa_usr, "")
        res = resolve_owner(empresa_id, pais_usr)
        rol = res["rol"]
        es_fallback = False
        if rol:
            # Mismo criterio que en Tickets: confiamos en el país registrado del dueño
            # real por sobre la columna "país" de la fila (evita descuadres entre el
            # total del país y la suma de los ejecutivos si esa columna viene mal tipeada).
            pais_bucket = res["pais"]
        else:
            usuarios_sin_asignar += 1
            key = normalize_empresa_id(empresa_id) or f"SIN_ID::{r.get(col_idtrib_usr, '')}"
            pais_bucket = pais_usr
            rol = pick_fallback_rol(pais_bucket, key)
            es_fallback = True
            if rol:
                usuarios_redistribuidos += 1
            entry = usuarios_sin_asignar_detalle.setdefault(key, {
                "empresa_id": normalize_empresa_id(empresa_id),
                "nombre_empresa": (r.get(col_nombre_empresa_usr, "") or "").strip(),
                "rut": r.get(col_idtrib_usr, ""),
                "pais": pais_usr,
                "motivo": res["motivo"],
                "email_kam_en_empresas": res["owner_email"],
                "redistribuido_a": rol,
                "usuarios_incentivados": 0,
            })
            entry["usuarios_incentivados"] += l_val
            if not rol:
                continue  # país sin regla de reparto -> se sigue excluyendo, como antes
        usuarios_total[rol][pais_bucket] += l_val
        if es_fallback:
            usuarios_pool_by_role_country[rol][pais_bucket] += l_val
        else:
            usuarios_by_exec[res["owner_email"]] += l_val

    # --- Reuniones: atribuir cada reunión a (rol, pais) por mes -----------------
    col_kamid_reu = find_header(reuniones_h, "KAM ID")
    col_seller_email = find_header(reuniones_h, "SELLER_EMAIL")
    col_anio = find_header(reuniones_h, "ANIO")
    col_mes = find_header(reuniones_h, "MES")

    reuniones_by_role_country_month = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    reuniones_by_exec_month = defaultdict(lambda: defaultdict(int))  # [correo][mk] = count
    all_month_keys = set()
    reuniones_sin_match = 0
    reuniones_sin_match_detalle = {}  # key: (kamid, email) -> count

    for r in reuniones_records:
        kamid = (r.get(col_kamid_reu, "") or "").strip()
        info = kamid_to_info.get(kamid) if kamid and kamid.upper() != "#N/A" else None
        correo_match = kamid_to_correo.get(kamid) if info else None
        if not info:
            # SELLER_EMAIL a veces trae varios correos separados por coma (artefacto de fórmula).
            # Probamos cada uno hasta encontrar un match en la Leyenda.
            email_raw = (r.get(col_seller_email, "") or "").strip()
            for candidate in email_raw.split(","):
                candidate = candidate.strip().lower()
                if candidate in email_to_info:
                    info = email_to_info[candidate]
                    correo_match = candidate
                    break
        if not info:
            reuniones_sin_match += 1
            email_val = (r.get(col_seller_email, "") or "").strip()
            key = f"{kamid}::{email_val}"
            entry = reuniones_sin_match_detalle.setdefault(key, {
                "kam_id": kamid,
                "seller_email": email_val,
                "count": 0,
            })
            entry["count"] += 1
            continue

        anio = parse_number(r.get(col_anio, ""))
        mes = parse_number(r.get(col_mes, ""))
        if not anio or not mes:
            continue

        mk = month_key(anio, mes)
        all_month_keys.add(mk)
        reuniones_by_role_country_month[info["rol"]][info["pais"]][mk] += 1
        if correo_match:
            reuniones_by_exec_month[correo_match][mk] += 1

    month_keys_sorted = sorted(all_month_keys)
    month_labels = [month_label(mk) for mk in month_keys_sorted]

    # --- Reuniones: promedio mensual y meses activos por rol/país (mismo criterio que tickets) ---
    reuniones_mensual = defaultdict(dict)
    reuniones_n_meses = defaultdict(dict)
    reuniones_monthly_by_role_country = defaultdict(dict)
    for rol in ROLES:
        for pais in COUNTRY_ORDER:
            monthly_counts = [
                reuniones_by_role_country_month[rol][pais].get(mk, 0) for mk in month_keys_sorted
            ]
            active = [v for v in monthly_counts if v > 0]
            reuniones_mensual[rol][pais] = round(sum(active) / len(active), 2) if active else 0
            reuniones_n_meses[rol][pais] = len(active)
            reuniones_monthly_by_role_country[rol][pais] = monthly_counts

    # --- Detalle por ejecutivo real (nombre) -------------------------------------
    # Lo redistribuido (pool) NO se muestra aparte: se reparte en partes iguales entre
    # los ejecutivos reales de ese rol/país y se suma directo a sus propios números,
    # usando el MISMO denominador de meses que el país (para que la suma de todos los
    # ejecutivos coincida con el total del país en las tarjetas/ranking).
    ejecutivos_detalle = defaultdict(lambda: defaultdict(list))  # [rol][pais] = [ {...}, ... ]
    for correo, info in correo_to_exec.items():
        rol, pais = info["rol"], info["pais"]
        n_exec = ejecutivos.get(rol, {}).get(pais, 0) or 1

        n_meses_tix = tickets_n_meses.get(rol, {}).get(pais, 0)
        own_tickets_sum = sum(tickets_by_exec_month.get(correo, {}).values())
        pool_tickets_sum = sum(tickets_pool_by_role_country_month[rol].get(pais, {}).values())
        own_tickets_mensual = (own_tickets_sum / n_meses_tix) if n_meses_tix else 0
        pool_tickets_mensual = (pool_tickets_sum / n_meses_tix) if n_meses_tix else 0

        n_meses_reu = reuniones_n_meses.get(rol, {}).get(pais, 0)
        own_reuniones_sum = sum(reuniones_by_exec_month.get(correo, {}).values())
        own_reuniones_mensual = (own_reuniones_sum / n_meses_reu) if n_meses_reu else 0

        own_clientes = len(clientes_by_exec.get(correo, set()))
        pool_clientes = len(clientes_pool_by_role_country[rol].get(pais, set()))

        own_usuarios = usuarios_by_exec.get(correo, 0)
        pool_usuarios = usuarios_pool_by_role_country[rol].get(pais, 0)

        ejecutivos_detalle[rol][pais].append({
            "nombre": info["nombre"],
            "correo": correo,
            "tickets_mensual": round(own_tickets_mensual + pool_tickets_mensual / n_exec, 2),
            "reuniones_mensual": round(own_reuniones_mensual, 2),
            "total_clientes": round(own_clientes + pool_clientes / n_exec, 2),
            "usuarios_total": round(own_usuarios + pool_usuarios / n_exec, 2),
        })
    for rol in ejecutivos_detalle:
        for pais in ejecutivos_detalle[rol]:
            ejecutivos_detalle[rol][pais].sort(key=lambda e: (-e["tickets_mensual"], e["nombre"]))

    # --- Construir estructura final ---------------------------------------------
    tabs = {}
    for rol in ROLES:
        countries = {}
        for pais in COUNTRY_ORDER:
            n_exec = ejecutivos.get(rol, {}).get(pais, 0)
            monthly_counts = reuniones_monthly_by_role_country[rol][pais]
            reuniones_mensual_v = reuniones_mensual[rol].get(pais, 0)

            t_mensual = tickets_mensual[rol].get(pais, 0)
            t_clientes = total_clientes[rol].get(pais, 0)
            u_total = round(usuarios_total[rol].get(pais, 0), 2)

            countries[pais] = {
                "flag": COUNTRY_FLAGS.get(pais, ""),
                "ejecutivos": n_exec,
                "tickets_mensual": t_mensual,
                "tickets_exec": safe_div(t_mensual, n_exec),
                "reuniones_monthly": monthly_counts,
                "reuniones_mensual": reuniones_mensual_v,
                "reuniones_exec": safe_div(reuniones_mensual_v, n_exec),
                "clientes_exec": safe_div(t_clientes, n_exec),
                "total_clientes": t_clientes,
                "usuarios_total": u_total,
                "usuarios_exec": safe_div(u_total, n_exec),
                "ejecutivos_detalle": ejecutivos_detalle.get(rol, {}).get(pais, []),
            }
        tabs[rol] = {"countries": countries, "country_order": COUNTRY_ORDER}

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "month_labels": month_labels,
        "country_order": COUNTRY_ORDER,
        "tabs": tabs,
        "meta": {
            "reuniones_sin_match": reuniones_sin_match,
            "tickets_sin_asignar": tickets_sin_asignar,
            "tickets_redistribuidos": tickets_redistribuidos,
            "tickets_excluidos": tickets_sin_asignar - tickets_redistribuidos,
            "usuarios_sin_asignar": usuarios_sin_asignar,
            "usuarios_redistribuidos": usuarios_redistribuidos,
            "usuarios_excluidos": usuarios_sin_asignar - usuarios_redistribuidos,
        },
    }

    unmatched_report = {
        "generated_at": output["generated_at"],
        "tickets_sin_asignar": sorted(tickets_sin_asignar_detalle.values(), key=lambda x: -x["count"]),
        "usuarios_sin_asignar": sorted(usuarios_sin_asignar_detalle.values(), key=lambda x: -x["usuarios_incentivados"]),
        "reuniones_sin_match": sorted(reuniones_sin_match_detalle.values(), key=lambda x: -x["count"]),
    }

    return output, unmatched_report


def main():
    gc = get_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    tickets_records, tickets_h = get_records(sh.worksheet("Tickets"))
    usuarios_records, usuarios_h = get_records(sh.worksheet("Usuarios"))
    reuniones_records, reuniones_h = get_records(sh.worksheet("Reuniones"))
    leyenda_records, leyenda_h = get_records(sh.worksheet("Leyenda"))

    empresa_owner_map = build_empresa_owner_map(gc)

    output, unmatched_report = build_dashboard_data(
        tickets_records, tickets_h,
        usuarios_records, usuarios_h,
        reuniones_records, reuniones_h,
        leyenda_records, leyenda_h,
        empresa_owner_map,
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    with open(UNMATCHED_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(unmatched_report, f, ensure_ascii=False, indent=2)

    meta = output["meta"]
    print(
        f"OK: data.json generado con {len(output['month_labels'])} meses. "
        f"Reuniones sin match: {meta['reuniones_sin_match']}. "
        f"Tickets sin dueño reconocido: {meta['tickets_sin_asignar']} "
        f"(redistribuidos: {meta['tickets_redistribuidos']}, excluidos: {meta['tickets_excluidos']}). "
        f"Usuarios sin dueño reconocido: {meta['usuarios_sin_asignar']} "
        f"(redistribuidos: {meta['usuarios_redistribuidos']}, excluidos: {meta['usuarios_excluidos']})."
    )


if __name__ == "__main__":
    main()
