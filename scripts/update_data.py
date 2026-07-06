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
    """Lee la spreadsheet 'Empresas (Todos los países)' y arma ID Empresa -> email dueño."""
    sh = gc.open_by_key(EMPRESAS_SPREADSHEET_ID)
    owner_map = {}
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
            owner_map[eid] = {"email": email, "pais": pais}
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

    kamid_to_info = {}
    email_to_info = {}
    ejecutivos_set = defaultdict(lambda: defaultdict(set))  # ejecutivos_set[rol][pais] = {emails}

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
            email_to_info[correo] = info
            ejecutivos_set[rol][pais].add(correo)

    ejecutivos = {rol: {pais: len(emails) for pais, emails in paises.items()} for rol, paises in ejecutivos_set.items()}

    def resolve_owner(empresa_id):
        """Dado un ID Empresa, intenta resolver el ejecutivo dueño (rol/pais).
        Devuelve dict con rol/pais si matchea, o motivo + email (si corresponde) si no."""
        eid = normalize_empresa_id(empresa_id)
        if not eid:
            return {"rol": None, "pais": None, "motivo": "sin_id_empresa", "owner_email": None}
        owner = empresa_owner_map.get(eid)
        if not owner:
            return {"rol": None, "pais": None, "motivo": "empresa_no_esta_en_hoja_empresas", "owner_email": None}
        info = email_to_info.get(owner["email"])
        if not info:
            return {"rol": None, "pais": None, "motivo": "email_kam_no_esta_en_leyenda", "owner_email": owner["email"]}
        return {"rol": info["rol"], "pais": info["pais"], "motivo": None, "owner_email": owner["email"]}

    # --- Tickets: atribuir cada ticket a (rol, pais) vía ID Empresa -------------
    col_pais_tix = find_header(tickets_h, "pais", "país")
    col_empresa_id = find_header(tickets_h, "empresa")
    col_idtrib = find_header(tickets_h, "id_tributario")
    col_fecha = find_header(tickets_h, "fechaCreacion")
    col_nombre_empresa_tix = find_header(tickets_h, "nombre empresa")

    tickets_by_role_country_month = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    clientes_by_role_country = defaultdict(lambda: defaultdict(set))
    tickets_sin_asignar = 0
    tickets_sin_asignar_detalle = {}  # key: empresa_id -> {..., count}

    for r in tickets_records:
        pais_ticket = normalize_country(r.get(col_pais_tix, ""))
        if not pais_ticket:
            continue
        empresa_id = r.get(col_empresa_id, "")
        res = resolve_owner(empresa_id)
        if not res["rol"]:
            tickets_sin_asignar += 1
            key = normalize_empresa_id(empresa_id) or f"SIN_ID::{r.get(col_idtrib, '')}"
            entry = tickets_sin_asignar_detalle.setdefault(key, {
                "empresa_id": normalize_empresa_id(empresa_id),
                "nombre_empresa": (r.get(col_nombre_empresa_tix, "") or "").strip(),
                "rut": r.get(col_idtrib, ""),
                "pais": pais_ticket,
                "motivo": res["motivo"],
                "email_kam_en_empresas": res["owner_email"],
                "count": 0,
            })
            entry["count"] += 1
            continue
        rol = res["rol"]

        id_trib = normalize_tax_id(r.get(col_idtrib, ""))
        if id_trib:
            clientes_by_role_country[rol][pais_ticket].add(id_trib)

        fecha = (r.get(col_fecha, "") or "").strip()
        if len(fecha) >= 10:
            try:
                dt = datetime.strptime(fecha[:10], "%Y-%m-%d")
                mk = month_key(dt.year, dt.month)
                tickets_by_role_country_month[rol][pais_ticket][mk] += 1
            except ValueError:
                pass

    tickets_mensual = defaultdict(dict)
    for rol in ROLES:
        for pais in COUNTRY_ORDER:
            months = tickets_by_role_country_month[rol].get(pais, {})
            active = [v for v in months.values() if v > 0]
            tickets_mensual[rol][pais] = round(sum(active) / len(active), 2) if active else 0

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
    usuarios_sin_asignar_detalle = {}

    for r in usuarios_records:
        l_val = parse_number(r.get(col_usuarios_l, ""))
        if l_val is None:
            continue  # columna L vacía => empresa sin compras hace +12 meses, se excluye
        pais_usr = normalize_country(r.get(col_pais_usr, ""))
        if not pais_usr:
            continue
        empresa_id = r.get(col_id_empresa_usr, "")
        res = resolve_owner(empresa_id)
        if not res["rol"]:
            usuarios_sin_asignar += 1
            key = normalize_empresa_id(empresa_id) or f"SIN_ID::{r.get(col_idtrib_usr, '')}"
            entry = usuarios_sin_asignar_detalle.setdefault(key, {
                "empresa_id": normalize_empresa_id(empresa_id),
                "nombre_empresa": (r.get(col_nombre_empresa_usr, "") or "").strip(),
                "rut": r.get(col_idtrib_usr, ""),
                "pais": pais_usr,
                "motivo": res["motivo"],
                "email_kam_en_empresas": res["owner_email"],
                "usuarios_incentivados": 0,
            })
            entry["usuarios_incentivados"] += l_val
            continue
        usuarios_total[res["rol"]][pais_usr] += l_val

    # --- Reuniones: atribuir cada reunión a (rol, pais) por mes -----------------
    col_kamid_reu = find_header(reuniones_h, "KAM ID")
    col_seller_email = find_header(reuniones_h, "SELLER_EMAIL")
    col_anio = find_header(reuniones_h, "ANIO")
    col_mes = find_header(reuniones_h, "MES")

    reuniones_by_role_country_month = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    all_month_keys = set()
    reuniones_sin_match = 0
    reuniones_sin_match_detalle = {}  # key: (kamid, email) -> count

    for r in reuniones_records:
        kamid = (r.get(col_kamid_reu, "") or "").strip()
        info = kamid_to_info.get(kamid) if kamid and kamid.upper() != "#N/A" else None
        if not info:
            email = (r.get(col_seller_email, "") or "").strip().lower()
            info = email_to_info.get(email)
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

    month_keys_sorted = sorted(all_month_keys)
    month_labels = [month_label(mk) for mk in month_keys_sorted]

    # --- Construir estructura final ---------------------------------------------
    tabs = {}
    for rol in ROLES:
        countries = {}
        for pais in COUNTRY_ORDER:
            n_exec = ejecutivos.get(rol, {}).get(pais, 0)
            monthly_counts = [
                reuniones_by_role_country_month[rol][pais].get(mk, 0) for mk in month_keys_sorted
            ]
            active = [v for v in monthly_counts if v > 0]
            reuniones_mensual_v = round(sum(active) / len(active), 2) if active else 0

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
            "usuarios_sin_asignar": usuarios_sin_asignar,
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

    print(
        f"OK: data.json generado con {len(output['month_labels'])} meses. "
        f"Reuniones sin match: {output['meta']['reuniones_sin_match']}, "
        f"Tickets sin asignar: {output['meta']['tickets_sin_asignar']}, "
        f"Usuarios sin asignar: {output['meta']['usuarios_sin_asignar']}."
    )


if __name__ == "__main__":
    main()
