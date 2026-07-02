"""
Genera data.json para el KAM Dashboard leyendo directamente desde Google Sheets.

Lee 5 pestañas de la misma spreadsheet:
  - Tickets   -> tickets/mes por país, y Q de clientes (ID tributario único) por país
  - Usuarios  -> usuarios incentivados (col "N° usuarios incentivados") por país
  - Reuniones -> reuniones por ejecutivo, atribuidas a país/rol vía Leyenda
  - Leyenda   -> mapea KAM ID / correo -> rol (KAM, BDM, Full Cycle) y país
  - Config    -> cantidad de ejecutivos por país/rol (editable desde el panel admin)

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
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")

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
    """minimiza un header a algo comparable: sin acentos, sin espacios, minúsculas"""
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_dashboard_data(
    tickets_records, tickets_h,
    usuarios_records, usuarios_h,
    reuniones_records, reuniones_h,
    leyenda_records, leyenda_h,
    config_records, config_h,
):
    """Lógica pura de agregación (sin I/O) — separada para poder testear con datos falsos."""

    # --- Leyenda: KAM ID / correo -> {rol, pais} -------------------------------
    col_correo = find_header(leyenda_h, "Correo", "Email")
    col_kamid = find_header(leyenda_h, "KAM ID")
    col_rol = find_header(leyenda_h, "Rol")
    col_pais_ley = find_header(leyenda_h, "Pais", "País")

    kamid_to_info = {}
    email_to_info = {}
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

    # --- Config: ejecutivos por rol/pais ---------------------------------------
    col_pais_cfg = find_header(config_h, "Pais", "País")
    col_rol_cfg = find_header(config_h, "Rol")
    col_exec_cfg = find_header(config_h, "Ejecutivos")

    ejecutivos = defaultdict(lambda: defaultdict(int))
    for r in config_records:
        pais = normalize_country(r.get(col_pais_cfg, ""))
        rol = (r.get(col_rol_cfg, "") or "").strip()
        n = parse_number(r.get(col_exec_cfg, "0")) or 0
        if pais and rol:
            ejecutivos[rol][pais] = int(n)

    # --- Tickets: conteo mensual por país + clientes únicos por país ----------
    col_pais_tix = find_header(tickets_h, "pais", "país")
    col_idtrib = find_header(tickets_h, "id_tributario")
    col_fecha = find_header(tickets_h, "fechaCreacion")

    tickets_by_country_month = defaultdict(lambda: defaultdict(int))
    clientes_by_country = defaultdict(set)

    for r in tickets_records:
        pais = normalize_country(r.get(col_pais_tix, ""))
        if not pais:
            continue
        id_trib = normalize_tax_id(r.get(col_idtrib, ""))
        if id_trib:
            clientes_by_country[pais].add(id_trib)
        fecha = (r.get(col_fecha, "") or "").strip()
        if len(fecha) >= 10:
            try:
                dt = datetime.strptime(fecha[:10], "%Y-%m-%d")
                mk = month_key(dt.year, dt.month)
                tickets_by_country_month[pais][mk] += 1
            except ValueError:
                pass

    tickets_mensual = {}
    for pais in COUNTRY_ORDER:
        months = tickets_by_country_month.get(pais, {})
        active = [v for v in months.values() if v > 0]
        tickets_mensual[pais] = round(sum(active) / len(active), 2) if active else 0

    total_clientes = {pais: len(clientes_by_country.get(pais, set())) for pais in COUNTRY_ORDER}

    # --- Usuarios: sum columna L (usuarios incentivados) por país --------------
    col_usuarios_l = find_header(usuarios_h, "N usuarios incentivados", "usuarios incentivados")
    col_pais_usr = find_header(usuarios_h, "Pais", "País")

    usuarios_total = defaultdict(float)
    for r in usuarios_records:
        l_val = parse_number(r.get(col_usuarios_l, ""))
        if l_val is None:
            continue  # columna L vacía => empresa sin compras hace +12 meses, se excluye
        pais = normalize_country(r.get(col_pais_usr, ""))
        if not pais:
            continue
        usuarios_total[pais] += l_val

    # --- Reuniones: atribuir cada reunión a (rol, pais) por mes -----------------
    col_kamid_reu = find_header(reuniones_h, "KAM ID")
    col_seller_email = find_header(reuniones_h, "SELLER_EMAIL")
    col_anio = find_header(reuniones_h, "ANIO")
    col_mes = find_header(reuniones_h, "MES")

    reuniones_by_role_country_month = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    all_month_keys = set()
    unmatched = 0

    for r in reuniones_records:
        kamid = (r.get(col_kamid_reu, "") or "").strip()
        info = kamid_to_info.get(kamid) if kamid and kamid.upper() != "#N/A" else None
        if not info:
            email = (r.get(col_seller_email, "") or "").strip().lower()
            info = email_to_info.get(email)
        if not info:
            unmatched += 1
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
            n_exec = ejecutivos[rol].get(pais, 0)
            monthly_counts = [
                reuniones_by_role_country_month[rol][pais].get(mk, 0) for mk in month_keys_sorted
            ]
            active = [v for v in monthly_counts if v > 0]
            reuniones_mensual_v = round(sum(active) / len(active), 2) if active else 0

            t_mensual = tickets_mensual.get(pais, 0)
            t_clientes = total_clientes.get(pais, 0)
            u_total = round(usuarios_total.get(pais, 0), 2)

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
            "reuniones_sin_match": unmatched,
        },
    }
    return output


def main():
    gc = get_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    tickets_records, tickets_h = get_records(sh.worksheet("Tickets"))
    usuarios_records, usuarios_h = get_records(sh.worksheet("Usuarios"))
    reuniones_records, reuniones_h = get_records(sh.worksheet("Reuniones"))
    leyenda_records, leyenda_h = get_records(sh.worksheet("Leyenda"))
    config_records, config_h = get_records(sh.worksheet("Config"))

    output = build_dashboard_data(
        tickets_records, tickets_h,
        usuarios_records, usuarios_h,
        reuniones_records, reuniones_h,
        leyenda_records, leyenda_h,
        config_records, config_h,
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"OK: data.json generado con {len(output['month_labels'])} meses. "
          f"Reuniones sin match: {output['meta']['reuniones_sin_match']}")


if __name__ == "__main__":
    main()
